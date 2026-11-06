import type { AuthStore } from "./authStore";
import { errorCodeOf } from "./errors";

export type ApiBase = "gw" | "ops";

/**
 * none: no Authorization header. user: gateway session. merchantKey: merchant API key (gateway merchant routes).
 * merchantOps: ops-ai merchant token; on a 401 the client refreshes it once via POST /v1/merchant-tokens with
 * the API key and repeats the request.
 */
export type ApiAuth = "none" | "user" | "merchantKey" | "merchantOps";

export interface ApiRequest {
  base: ApiBase;
  /** Service path, e.g. "/v1/me/wallets"; the client prefixes "/api/gw" or "/api/ops". */
  path: string;
  method?: "GET" | "POST";
  auth: ApiAuth;
  body?: unknown;
  query?: Record<string, string | number | undefined>;
  /** Sent as the Idempotency-Key header, unchanged on every retry of this request. */
  idempotencyKey?: string;
  /** Extra attempts after a network error or a 502/503/504 (default 0). */
  retries?: number;
}

export type ApiResult<T> =
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; code: string; body: unknown };

export interface ApiClient {
  request<T>(req: ApiRequest): Promise<ApiResult<T>>;
}

export interface ApiClientDeps {
  fetch: typeof fetch;
  store: AuthStore;
}

/**
 * A 401 on a user request clears the whole store; a 401 on a merchant request (after the one merchantOps
 * refresh) clears only the merchant credentials.
 * A network failure that exhausts its retries resolves to { ok: false, status: 0, code: "network_error" }.
 * A credentialed request whose credential is missing resolves to { ok: false, status: 401, code:
 * "not_signed_in" } without calling fetch.
 */
export function createApiClient(deps: ApiClientDeps): ApiClient {
  const { store } = deps;

  function credentialFor(auth: ApiAuth): string | null {
    const state = store.get();
    if (auth === "user") return state.userSessionToken;
    if (auth === "merchantKey") return state.merchantApiKey;
    if (auth === "merchantOps") return state.merchantOpsToken === "" ? null : state.merchantOpsToken;
    return null;
  }

  function send<T>(req: ApiRequest, token: string | null): Promise<ApiResult<T>> {
    return sendRequest<T>(deps.fetch, req, token);
  }

  async function refreshOpsToken(): Promise<string | null> {
    const apiKey = store.get().merchantApiKey;
    if (apiKey === null) return null;
    const result = await send<{ access_token: string }>(
      { base: "gw", path: "/v1/merchant-tokens", method: "POST", auth: "merchantKey" },
      apiKey,
    );
    if (!result.ok || typeof result.data.access_token !== "string") return null;
    store.setMerchantOpsToken(result.data.access_token);
    return result.data.access_token;
  }

  return {
    async request<T>(req: ApiRequest): Promise<ApiResult<T>> {
      const token = credentialFor(req.auth);
      if (req.auth !== "none" && token === null) return NOT_SIGNED_IN;
      let result = await send<T>(req, token);
      if (result.ok || result.status !== 401 || req.auth === "none") return result;
      if (req.auth === "merchantOps") {
        const fresh = await refreshOpsToken();
        if (fresh !== null) result = await send<T>(req, fresh);
        if (result.ok || result.status !== 401) return result;
      }
      if (req.auth === "user") store.clear();
      else store.clearMerchant();
      return result;
    },
  };
}

/** One request with an explicit bearer token (or none), retried per `req.retries`; shared by the ops client. */
export async function sendRequest<T>(
  fetchFn: typeof fetch,
  req: Omit<ApiRequest, "auth">,
  token: string | null,
): Promise<ApiResult<T>> {
  const url = buildUrl(req);
  const headers: Record<string, string> = {};
  if (token !== null) headers["Authorization"] = `Bearer ${token}`;
  if (req.idempotencyKey !== undefined) headers["Idempotency-Key"] = req.idempotencyKey;
  const init: RequestInit = { method: req.method ?? "GET", headers, cache: "no-store" };
  if (req.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(req.body);
  }
  const attempts = 1 + Math.max(0, req.retries ?? 0);
  let last: ApiResult<T> = NETWORK_ERROR;
  for (let attempt = 0; attempt < attempts; attempt++) {
    let response: Response;
    try {
      response = await fetchFn(url, init);
      last = await toResult<T>(response);
    } catch {
      last = NETWORK_ERROR;
      continue;
    }
    if (!RETRYABLE.has(response.status)) return last;
  }
  return last;
}

const RETRYABLE: ReadonlySet<number> = new Set([502, 503, 504]);
const NETWORK_ERROR = { ok: false, status: 0, code: "network_error", body: null } as const;
export const NOT_SIGNED_IN = { ok: false, status: 401, code: "not_signed_in", body: null } as const;
const PREFIX: Record<ApiBase, string> = { gw: "/api/gw", ops: "/api/ops" };

function buildUrl(req: Omit<ApiRequest, "auth">): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(req.query ?? {})) {
    if (value !== undefined) params.append(key, String(value));
  }
  const query = params.toString();
  return `${PREFIX[req.base]}${req.path}${query === "" ? "" : `?${query}`}`;
}

async function toResult<T>(response: Response): Promise<ApiResult<T>> {
  const text = await response.text();
  let body: unknown = null;
  if (text !== "") {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (response.ok) return { ok: true, status: response.status, data: body as T };
  return { ok: false, status: response.status, code: errorCodeOf(response.status, body), body };
}

/** One key per confirmed money action (decision 8). */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
