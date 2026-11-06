import { NOT_SIGNED_IN, sendRequest, type ApiBase, type ApiRequest, type ApiResult } from "./api";

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

/** Each credential goes only to the service that issued or accepts it: the session to gw, the ops-ai token to ops. */
export type OpsApiRequest = Omit<ApiRequest, "auth" | "base"> &
  ({ auth: "none"; base: ApiBase } | { auth: "opsSession"; base: "gw" } | { auth: "opsAi"; base: "ops" });

const CREDENTIAL_BASE: Record<Exclude<OpsAuth, "none">, ApiBase> = { opsSession: "gw", opsAi: "ops" };

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

/** token: a new ops-ai token. rejected: the session itself answered 401. failed: anything else (503, network, 403). */
type RefreshOutcome = { kind: "token"; token: string } | { kind: "rejected" } | { kind: "failed" };

/**
 * A 401 on an opsAi request refreshes the ops-ai token once with the ops session (concurrent 401s of the same
 * session share one refresh) and repeats the request; a 401 that survives, a 401 on the refresh, or a 401 on a
 * session request signs the ops user out. A refresh that fails for another reason keeps the session and returns
 * the original 401. A 403 (role outside the route policy) keeps the session. Every request remembers the session it
 * started under and never clears or refreshes a different (newer) session when its answer arrives late.
 */
export function createOpsApiClient(deps: OpsApiClientDeps): OpsApiClient {
  const { store } = deps;
  const refreshing = new Map<string, Promise<RefreshOutcome>>();

  function credentialFor(auth: OpsAuth): string | null {
    const state = store.get();
    if (auth === "opsSession") return state.sessionToken;
    if (auth === "opsAi") return state.opsAiToken;
    return null;
  }

  function stillSignedInAs(sessionToken: string | null): boolean {
    return sessionToken !== null && store.get().sessionToken === sessionToken;
  }

  async function issueOpsAiToken(sessionToken: string): Promise<ApiResult<TokenResponse>> {
    return sendRequest<TokenResponse>(deps.fetch, { base: "gw", path: "/v1/ops/ops-ai-tokens", method: "POST" }, sessionToken);
  }

  async function refreshOpsAiToken(sessionToken: string): Promise<RefreshOutcome> {
    const result = await issueOpsAiToken(sessionToken);
    if (!result.ok) return result.status === 401 ? { kind: "rejected" } : { kind: "failed" };
    if (typeof result.data.access_token !== "string") return { kind: "failed" };
    if (stillSignedInAs(sessionToken)) store.setOpsAiToken(result.data.access_token);
    return { kind: "token", token: result.data.access_token };
  }

  function sharedRefresh(sessionToken: string): Promise<RefreshOutcome> {
    let pending = refreshing.get(sessionToken);
    if (pending === undefined) {
      pending = refreshOpsAiToken(sessionToken).finally(() => {
        refreshing.delete(sessionToken);
      });
      refreshing.set(sessionToken, pending);
    }
    return pending;
  }

  function signOutIfStill(sessionToken: string | null): void {
    if (stillSignedInAs(sessionToken)) store.clear();
  }

  async function send<T>(req: OpsApiRequest, token: string | null, sessionAtStart: string | null): Promise<ApiResult<T>> {
    const result = await sendRequest<T>(deps.fetch, req, token);
    if (result.ok || result.status !== 401 || req.auth === "none") return result;
    if (sessionAtStart === null || !stillSignedInAs(sessionAtStart)) return result;
    if (req.auth === "opsSession") {
      store.clear();
      return result;
    }
    const refresh = await sharedRefresh(sessionAtStart);
    if (refresh.kind === "failed" || !stillSignedInAs(sessionAtStart)) return result;
    if (refresh.kind === "rejected") {
      store.clear();
      return result;
    }
    const repeated = await sendRequest<T>(deps.fetch, req, refresh.token);
    if (!repeated.ok && repeated.status === 401) signOutIfStill(sessionAtStart);
    return repeated;
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

    request<T>(req: OpsApiRequest): Promise<ApiResult<T>> {
      if (req.auth !== "none" && req.base !== CREDENTIAL_BASE[req.auth]) {
        throw new Error(`ops credential ${req.auth} is only sent to ${CREDENTIAL_BASE[req.auth]}`);
      }
      const token = credentialFor(req.auth);
      if (req.auth !== "none" && token === null) return Promise.resolve(NOT_SIGNED_IN);
      return send<T>(req, token, store.get().sessionToken);
    },
  };
}
