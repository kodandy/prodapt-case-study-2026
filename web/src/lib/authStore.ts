export interface AuthState {
  /** Gateway session (aud gateway) for /v1/me/*. */
  userSessionToken: string | null;
  /** Demo only: the merchant API key, sent to the gateway merchant routes (docs/specs/web.md decision 5). */
  merchantApiKey: string | null;
  /** Short-lived ops-ai merchant token from POST /v1/merchant-tokens. */
  merchantOpsToken: string | null;
}

export type AuthListener = (state: AuthState) => void;

/** Memory only: never storage, cookies, the URL or the console (decision 4). */
export interface AuthStore {
  get(): AuthState;
  setUserSession(token: string): void;
  setMerchant(apiKey: string, opsToken: string): void;
  setMerchantOpsToken(opsToken: string): void;
  clear(): void;
  /** Drops only the merchant key and ops token, so a merchant-side failure never signs the user out. */
  clearMerchant(): void;
  /** Called after every change; returns an unsubscribe function. */
  subscribe(listener: AuthListener): () => void;
}

const EMPTY: AuthState = { userSessionToken: null, merchantApiKey: null, merchantOpsToken: null };

export function createAuthStore(): AuthStore {
  let state = EMPTY;
  const listeners = new Set<AuthListener>();

  function set(next: AuthState): void {
    state = next;
    for (const listener of [...listeners]) listener(state);
  }

  return {
    get: () => state,
    setUserSession: (token) => set({ ...state, userSessionToken: token }),
    setMerchant: (apiKey, opsToken) => set({ ...state, merchantApiKey: apiKey, merchantOpsToken: opsToken }),
    setMerchantOpsToken: (opsToken) => set({ ...state, merchantOpsToken: opsToken }),
    clear: () => set(EMPTY),
    clearMerchant: () => set({ ...state, merchantApiKey: null, merchantOpsToken: null }),
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}
