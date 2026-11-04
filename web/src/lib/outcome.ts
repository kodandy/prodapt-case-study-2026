import type { ApiResult } from "./api";

/** True when the write may have been applied server-side, so the same Idempotency-Key must be reused (decision 8). */
export function isOutcomeUnknown(result: ApiResult<unknown>): boolean {
  if (result.ok) return false;
  if (result.code === "idempotency_in_progress") return true;
  return result.status === 0 || result.status >= 500 || result.status === 408 || result.status === 429;
}
