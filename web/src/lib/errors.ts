/**
 * The services answer errors in two shapes: `{"error": "<code>", ...}` (domain outcomes, 422 validation) and
 * FastAPI's `{"detail": "<message>"}`. errorCodeOf picks `error`, else a string `detail`, else `http_<status>`.
 */
export function errorCodeOf(status: number, body: unknown): string {
  if (typeof body === "object" && body !== null) {
    if ("error" in body && typeof body.error === "string") return body.error;
    if ("detail" in body && typeof body.detail === "string") return body.detail;
  }
  return `http_${status}`;
}

export const GENERIC_ERROR_MESSAGE = "Something went wrong. Please try again.";

const MESSAGES: ReadonlyMap<string, string> = new Map([
  ["insufficient_funds", "Not enough balance in this wallet."],
  ["recipient_not_found", "No user with that email."],
  ["self_transfer", "You cannot send money to yourself."],
  ["idempotency_key_reused", "This request was already used for a different action. Start again."],
  ["idempotency_in_progress", "This request is still being processed. Check again shortly."],
  ["payment_not_refundable", "This payment cannot be refunded."],
  ["refund_exceeds_refundable", "The refund is larger than the refundable amount."],
  ["payment_not_found", "Payment not found."],
  ["email already registered", "An account with this email already exists."],
  ["invalid credentials", "Email or password is incorrect."],
  ["mfa required", "Enter the 6-digit code from your authenticator app."],
  ["mfa locked", "Too many failed codes. Try again later."],
  ["invalid code", "That code is not valid. Check your authenticator app."],
  ["invalid api key", "The merchant API key was not accepted."],
  ["totp already enabled", "Two-factor authentication is already on."],
  ["try again later", "The service is busy. Try again later."],
  ["network_error", "Could not reach the server. Check your connection and try again."],
  ["not_signed_in", "Please sign in first."],
  ["validation_failed", "Some fields are not valid."],
]);

/** A user-facing message for a code; unknown codes get a generic message that never echoes the code. */
export function messageFor(code: string): string {
  return MESSAGES.get(code) ?? GENERIC_ERROR_MESSAGE;
}
