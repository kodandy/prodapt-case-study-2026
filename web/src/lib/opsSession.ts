import { NOT_SIGNED_IN, sendRequest, type ApiRequest, type ApiResult } from "./api";

export interface OpsSessionState {
  /** Gateway ops session (aud gateway-ops, 15 min) from POST /v1/ops/sessions; password + TOTP. */
  sessionToken: string | null;
  /** The role the gateway returned with the session ("ops_agent" | "ops_lead"); display only. */
  role: string | null;
  /** ops-ai ops token (15 min) from POST /v1/ops/ops-ai-tokens; sent to /api/ops/v1/ops/*. */
  opsAiToken: string | null;
}

export type OpsSessionListener = (state: OpsSessionState) => void;

/** Memory only, like the merchant/user store: never storage, cookies, the URL or the console. */
export interface OpsSessionStore {
  get(): OpsSessionState;
  setSession(sessionToken: string, role: string): void;
  setOpsAiToken(token: string): void;
  clear(): void;
  subscribe(listener: OpsSessionListener): () => void;
}

const SIGNED_OUT: OpsSessionState = { sessionToken: null, role: null, opsAiToken: null };

export function createOpsSessionStore(): OpsSessionStore {
  let state = SIGNED_OUT;
  const listeners = new Set<OpsSessionListener>();

  function set(next: OpsSessionState): void {
    state = next;
    for (const listener of [...listeners]) listener(state);
  }

  return {
    get: () => state,
    setSession: (sessionToken, role) => set({ sessionToken, role, opsAiToken: null }),
    setOpsAiToken: (opsAiToken) => set({ ...state, opsAiToken }),
    clear: () => set(SIGNED_OUT),
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/** none: no Authorization. opsSession: the gateway ops session. opsAi: the ops-ai ops token. */
export type OpsAuth = "none" | "opsSession" | "opsAi";

export interface OpsApiRequest extends Omit<ApiRequest, "auth"> {
  auth: OpsAuth;
}

export interface OpsCredentials {
  email: string;
  password: string;
  totpCode: string;
}

export type OpsSignInOutcome = { ok: true } | { ok: false; code: string };

export interface OpsApiClient {
  /** Password + TOTP login, then the ops-ai token; on any failure the store stays signed out. */
  signIn(credentials: OpsCredentials): Promise<OpsSignInOutcome>;
  request<T>(req: OpsApiRequest): Promise<ApiResult<T>>;
}

export interface OpsApiClientDeps {
  fetch: typeof fetch;
  store: OpsSessionStore;
}

interface OpsSessionResponse {
  access_token: string;
  role: string;
}

interface TokenResponse {
  access_token: string;
}

const TOTP_CODE = /^\d{6}$/;

/**
 * A 401 on an opsAi request refreshes the ops-ai token once with the ops session (concurrent 401s share one
 * refresh) and repeats the request; a 401 that survives, or any 401 on the session, signs the ops user out.
 * A 403 (role outside the route policy) keeps the session.
 */
export function createOpsApiClient(deps: OpsApiClientDeps): OpsApiClient {
  const { store } = deps;
  let refreshing: Promise<string | null> | null = null;

  function credentialFor(auth: OpsAuth): string | null {
    const state = store.get();
    if (auth === "opsSession") return state.sessionToken;
    if (auth === "opsAi") return state.opsAiToken;
    return null;
  }

  async function issueOpsAiToken(sessionToken: string): Promise<ApiResult<TokenResponse>> {
    return sendRequest<TokenResponse>(deps.fetch, { base: "gw", path: "/v1/ops/ops-ai-tokens", method: "POST" }, sessionToken);
  }

  async function refreshOpsAiToken(): Promise<string | null> {
    const sessionToken = store.get().sessionToken;
    if (sessionToken === null) return null;
    const result = await issueOpsAiToken(sessionToken);
    if (!result.ok || typeof result.data.access_token !== "string") return null;
    if (store.get().sessionToken !== sessionToken) return null;
    store.setOpsAiToken(result.data.access_token);
    return result.data.access_token;
  }

  function sharedRefresh(): Promise<string | null> {
    if (refreshing === null) {
      refreshing = refreshOpsAiToken().finally(() => {
        refreshing = null;
      });
    }
    return refreshing;
  }

  return {
    async signIn(credentials) {
      const email = credentials.email.trim();
      const totpCode = credentials.totpCode.trim();
      if (email === "" || credentials.password === "") return { ok: false, code: "missing_credentials" };
      if (!TOTP_CODE.test(totpCode)) return { ok: false, code: "totp_required" };
      store.clear();
      const login = await sendRequest<OpsSessionResponse>(
        deps.fetch,
        {
          base: "gw",
          path: "/v1/ops/sessions",
          method: "POST",
          body: { email, password: credentials.password, totp_code: totpCode },
        },
        null,
      );
      if (!login.ok) return { ok: false, code: login.code };
      if (typeof login.data.access_token !== "string" || typeof login.data.role !== "string") {
        return { ok: false, code: "invalid_response" };
      }
      const issued = await issueOpsAiToken(login.data.access_token);
      if (!issued.ok) return { ok: false, code: issued.code };
      if (typeof issued.data.access_token !== "string") return { ok: false, code: "invalid_response" };
      store.setSession(login.data.access_token, login.data.role);
      store.setOpsAiToken(issued.data.access_token);
      return { ok: true };
    },

    async request<T>(req: OpsApiRequest): Promise<ApiResult<T>> {
      const token = credentialFor(req.auth);
      if (req.auth !== "none" && token === null) return NOT_SIGNED_IN;
      let result = await sendRequest<T>(deps.fetch, req, token);
      if (result.ok || result.status !== 401 || req.auth === "none") return result;
      if (req.auth === "opsAi") {
        const fresh = await sharedRefresh();
        if (fresh !== null) result = await sendRequest<T>(deps.fetch, req, fresh);
        if (result.ok || result.status !== 401) return result;
      }
      store.clear();
      return result;
    },
  };
}
