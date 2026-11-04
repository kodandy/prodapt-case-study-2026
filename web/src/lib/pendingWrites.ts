import { newIdempotencyKey } from "./api";
import type { AuthStore } from "./authStore";

/**
 * Idempotency-Keys of money writes whose final outcome is not known yet, kept outside the components so an
 * unmount (tab switch, filter change, Cancel/Close) cannot mint a second key for the same write (decision 8).
 * Scopes start with "user:" or "merchant:" and are dropped when that credential is cleared.
 */
export interface PendingWrites {
  keyFor(scope: string, body: unknown): string;
  /** With key, removes the entry only while it still holds that key, so a stale response cannot drop a newer one. */
  settle(scope: string, body: unknown, key?: string): void;
}

export function createPendingWrites(store: AuthStore): PendingWrites {
  const keys = new Map<string, string>();

  function dropScopes(prefix: string): void {
    for (const entry of [...keys.keys()]) {
      if (entry.startsWith(prefix)) keys.delete(entry);
    }
  }

  store.subscribe((state) => {
    if (state.userSessionToken === null) dropScopes("user:");
    if (state.merchantApiKey === null) dropScopes("merchant:");
  });

  return {
    keyFor(scope, body) {
      const entry = entryOf(scope, body);
      const existing = keys.get(entry);
      if (existing !== undefined) return existing;
      const key = newIdempotencyKey();
      keys.set(entry, key);
      return key;
    },
    settle(scope, body, key) {
      const entry = entryOf(scope, body);
      if (key === undefined || keys.get(entry) === key) keys.delete(entry);
    },
  };
}

function entryOf(scope: string, body: unknown): string {
  return `${scope}\n${JSON.stringify(canonical(body))}`;
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === "object") {
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) sorted[key] = canonical((value as Record<string, unknown>)[key]);
    return sorted;
  }
  return value;
}
