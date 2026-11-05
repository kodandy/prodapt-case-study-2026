import { useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { ConfirmButton } from "../../components/ConfirmButton";
import { CURRENCIES, formatMinor, parseToMinor, type Currency } from "../../lib/money";
import { isOutcomeUnknown } from "../../lib/outcome";
import type { ConsumerChargeResult } from "./types";

interface CardInput {
  number: string;
  exp_month: number;
  exp_year: number;
  cvc: string;
}

interface Draft {
  merchantId: string;
  amountMinor: number;
  currency: Currency;
  card: CardInput;
  idempotencyKey: string;
}

const SCOPE = "user:pay-merchant";

const PARSE_MESSAGES = {
  empty: "Enter an amount.",
  invalid: "Enter an amount like 12.50.",
  too_many_decimals: "Too many decimal places for this currency.",
  not_positive: "The amount must be more than zero.",
} as const;

const ERROR_MESSAGES: ReadonlyMap<string, string> = new Map([["merchant_not_found", "No merchant with that ID."]]);

function bodyOf(draft: Omit<Draft, "idempotencyKey">) {
  return { merchant_id: draft.merchantId, amount_minor: draft.amountMinor, currency: draft.currency, card: draft.card };
}

/** What identifies "the same payment" for key reuse; the full card number and CVC are never kept as a key. */
function keyIdentityOf(draft: Omit<Draft, "idempotencyKey">) {
  return {
    merchant_id: draft.merchantId,
    amount_minor: draft.amountMinor,
    currency: draft.currency,
    card: { last4: draft.card.number.slice(-4), exp_month: draft.card.exp_month, exp_year: draft.card.exp_year },
  };
}

function parseMonth(text: string): number | null {
  const value = Number(text.trim());
  return Number.isInteger(value) && value >= 1 && value <= 12 ? value : null;
}

function parseYear(text: string): number | null {
  const value = Number(text.trim());
  return Number.isInteger(value) && value >= 2000 && value <= 2100 ? value : null;
}

/** B13.1: pay a merchant by card with an explicit confirmation step; the key is reused until a final answer. */
export function PayMerchantPanel(props: { onPaid: () => void }): ReactNode {
  const { client, pendingWrites } = useServices();
  const [merchantId, setMerchantId] = useState("");
  const [amountText, setAmountText] = useState("");
  const [currency, setCurrency] = useState<Currency>("USD");
  const [cardNumber, setCardNumber] = useState("");
  const [expMonth, setExpMonth] = useState("");
  const [expYear, setExpYear] = useState("");
  const [cvc, setCvc] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  function review(): void {
    const parsed = parseToMinor(amountText, currency);
    if (!parsed.ok) {
      setMessage(PARSE_MESSAGES[parsed.reason]);
      return;
    }
    const merchant = merchantId.trim().toLowerCase();
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(merchant)) {
      setMessage("Enter the merchant ID.");
      return;
    }
    const number = cardNumber.replace(/[\s-]/g, "");
    const month = parseMonth(expMonth);
    const year = parseYear(expYear);
    if (!/^[0-9]{12,19}$/.test(number) || month === null || year === null || !/^[0-9]{3,4}$/.test(cvc.trim())) {
      setMessage("Check the card details.");
      return;
    }
    setMessage(null);
    const next = {
      merchantId: merchant,
      amountMinor: parsed.amountMinor,
      currency,
      card: { number, exp_month: month, exp_year: year, cvc: cvc.trim() },
    };
    setDraft({ ...next, idempotencyKey: pendingWrites.keyFor(SCOPE, keyIdentityOf(next)) });
  }

  async function send(current: Draft): Promise<void> {
    setSending(true);
    const result = await client.request<ConsumerChargeResult>({
      base: "gw",
      path: "/v1/me/payments",
      method: "POST",
      auth: "user",
      body: bodyOf(current),
      idempotencyKey: current.idempotencyKey,
      retries: 1,
    });
    setSending(false);
    if (isOutcomeUnknown(result)) {
      setMessage(`${errorMessageOf(result) ?? ""} Press Confirm again to retry the same payment.`);
      return;
    }
    pendingWrites.settle(SCOPE, keyIdentityOf(current), current.idempotencyKey);
    setDraft(null);
    setCardNumber("");
    setCvc("");
    if (!result.ok) {
      setMessage(ERROR_MESSAGES.get(result.code) ?? errorMessageOf(result));
      return;
    }
    if (result.data.state === "succeeded") {
      setAmountText("");
      setMessage(`Paid ${formatMinor(result.data.amount_minor, result.data.currency)}.`);
      props.onPaid();
    } else {
      setMessage("The payment was declined. Try another card.");
    }
  }

  return (
    <section aria-labelledby="pay-merchant-title">
      <h2 id="pay-merchant-title">Pay a merchant</h2>
      {draft === null ? (
        <div className="form">
          <label>
            Merchant ID
            <input value={merchantId} onChange={(e) => setMerchantId(e.target.value)} />
          </label>
          <label>
            Amount
            <input inputMode="decimal" value={amountText} onChange={(e) => setAmountText(e.target.value)} />
          </label>
          <label>
            Currency
            <select value={currency} onChange={(e) => setCurrency(e.target.value as Currency)}>
              {CURRENCIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <label>
            Card number
            <input
              inputMode="numeric"
              autoComplete="cc-number"
              value={cardNumber}
              onChange={(e) => setCardNumber(e.target.value)}
            />
          </label>
          <label>
            Expiry month
            <input inputMode="numeric" value={expMonth} onChange={(e) => setExpMonth(e.target.value)} />
          </label>
          <label>
            Expiry year
            <input inputMode="numeric" value={expYear} onChange={(e) => setExpYear(e.target.value)} />
          </label>
          <label>
            CVC
            <input type="password" inputMode="numeric" autoComplete="cc-csc" value={cvc} onChange={(e) => setCvc(e.target.value)} />
          </label>
          <button type="button" onClick={review}>
            Review
          </button>
        </div>
      ) : (
        <div className="confirm" role="group" aria-label="Confirm payment">
          <p>
            Pay <strong>{formatMinor(draft.amountMinor, draft.currency)}</strong> to merchant{" "}
            <strong>{draft.merchantId}</strong> with the card ending {draft.card.number.slice(-4)}?
          </p>
          <ConfirmButton onConfirm={() => send(draft)}>Confirm payment</ConfirmButton>
          <button type="button" className="link" disabled={sending} onClick={() => setDraft(null)}>
            Cancel
          </button>
        </div>
      )}
      {message === null ? null : <p role="status">{message}</p>}
    </section>
  );
}
