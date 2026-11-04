export type Currency = "USD" | "EUR" | "JPY" | "INR";

export const CURRENCIES: readonly Currency[] = ["USD", "EUR", "JPY", "INR"];

export type ParseFailure = "empty" | "invalid" | "too_many_decimals" | "not_positive";

export type ParseResult = { ok: true; amountMinor: number } | { ok: false; reason: ParseFailure };

const MINOR_DIGITS: Record<Currency, number> = { USD: 2, EUR: 2, JPY: 0, INR: 2 };
const AMOUNT = /^([0-9]+)(?:\.([0-9]+))?$/;

/** JPY 0, USD/EUR/INR 2. */
export function minorDigits(currency: Currency): number {
  return MINOR_DIGITS[currency];
}

/** Integer minor units → localized string via Intl.NumberFormat; exact for every amount below 2^53 / 10^digits. */
export function formatMinor(amountMinor: number, currency: Currency, locale = "en-US"): string {
  const digits = minorDigits(currency);
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(amountMinor / 10 ** digits);
}

/**
 * User text → positive integer minor units, without floating point. Accepts digits with an optional single
 * `.` and at most minorDigits(currency) decimals; surrounding whitespace is trimmed. Rejects signs,
 * exponents, thousands separators, and anything above Number.MAX_SAFE_INTEGER.
 */
export function parseToMinor(input: string, currency: Currency): ParseResult {
  const text = input.trim();
  if (text === "") return { ok: false, reason: "empty" };
  const match = AMOUNT.exec(text);
  if (match === null) return { ok: false, reason: "invalid" };
  const whole = match[1] ?? "";
  const fraction = match[2] ?? "";
  const digits = minorDigits(currency);
  if (fraction.length > digits) return { ok: false, reason: "too_many_decimals" };
  const minor = BigInt(whole + fraction.padEnd(digits, "0"));
  if (minor > BigInt(Number.MAX_SAFE_INTEGER)) return { ok: false, reason: "invalid" };
  if (minor === 0n) return { ok: false, reason: "not_positive" };
  return { ok: true, amountMinor: Number(minor) };
}
