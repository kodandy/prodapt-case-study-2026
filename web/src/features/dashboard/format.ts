import { CURRENCIES, formatMinor, type Currency } from "../../lib/money";

function isCurrency(code: string): code is Currency {
  return (CURRENCIES as readonly string[]).includes(code);
}

/** formatMinor for the supported currencies; any other code is shown as raw minor units, never guessed. */
export function formatAmount(amountMinor: number, currency: string): string {
  return isCurrency(currency) ? formatMinor(amountMinor, currency) : `${amountMinor} ${currency} (minor units)`;
}

/** A 0..1 ratio as a percent with one decimal, e.g. 0.123 → "12.3%". */
export function formatPercent(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}

/** "2026-11-05T09:45:00Z" → "2026-11-05 09:45 UTC". */
export function formatUtcMinute(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return `${at.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}
