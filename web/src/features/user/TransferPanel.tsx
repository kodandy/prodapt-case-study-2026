import { useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { ConfirmButton } from "../../components/ConfirmButton";
import { isOutcomeUnknown } from "../../lib/outcome";
import { CURRENCIES, formatMinor, parseToMinor, type Currency } from "../../lib/money";
import type { TransferResult } from "./types";

interface Draft {
  recipientEmail: string;
  amountMinor: number;
  currency: Currency;
  idempotencyKey: string;
}

const SCOPE = "user:transfer";

function bodyOf(draft: Omit<Draft, "idempotencyKey">) {
  return { recipient_email: draft.recipientEmail, amount_minor: draft.amountMinor, currency: draft.currency };
}

const PARSE_MESSAGES = {
  empty: "Enter an amount.",
  invalid: "Enter an amount like 12.50.",
  too_many_decimals: "Too many decimal places for this currency.",
  not_positive: "The amount must be more than zero.",
} as const;

/** P2P transfer with an explicit confirmation step; the key is reused until the server gives a final answer. */
export function TransferPanel(props: { onTransferred: () => void }): ReactNode {
  const { client, pendingWrites } = useServices();
  const [recipient, setRecipient] = useState("");
  const [amountText, setAmountText] = useState("");
  const [currency, setCurrency] = useState<Currency>("USD");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  function review(): void {
    const parsed = parseToMinor(amountText, currency);
    if (!parsed.ok) {
      setMessage(PARSE_MESSAGES[parsed.reason]);
      return;
    }
    if (recipient.trim() === "") {
      setMessage("Enter the recipient's email.");
      return;
    }
    setMessage(null);
    // Same normalisation as the gateway, so the key follows what the server treats as the same transfer.
    const next = { recipientEmail: recipient.trim().toLowerCase(), amountMinor: parsed.amountMinor, currency };
    setDraft({ ...next, idempotencyKey: pendingWrites.keyFor(SCOPE, bodyOf(next)) });
  }

  async function send(current: Draft): Promise<void> {
    setSending(true);
    const result = await client.request<TransferResult>({
      base: "gw",
      path: "/v1/me/transfers",
      method: "POST",
      auth: "user",
      body: bodyOf(current),
      idempotencyKey: current.idempotencyKey,
      retries: 1,
    });
    setSending(false);
    if (isOutcomeUnknown(result)) {
      setMessage(`${errorMessageOf(result)} Press Confirm again to retry the same transfer.`);
      return;
    }
    pendingWrites.settle(SCOPE, bodyOf(current), current.idempotencyKey);
    setDraft(null);
    if (result.ok) {
      setAmountText("");
      setMessage(`Sent ${formatMinor(result.data.amount_minor, result.data.currency)}.`);
      props.onTransferred();
    } else {
      setMessage(errorMessageOf(result));
    }
  }

  return (
    <section aria-labelledby="transfer-title">
      <h2 id="transfer-title">Send money</h2>
      {draft === null ? (
        <div className="form">
          <label>
            Recipient email
            <input type="email" value={recipient} onChange={(e) => setRecipient(e.target.value)} />
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
          <button type="button" onClick={review}>
            Review
          </button>
        </div>
      ) : (
        <div className="confirm" role="group" aria-label="Confirm transfer">
          <p>
            Send <strong>{formatMinor(draft.amountMinor, draft.currency)}</strong> to{" "}
            <strong>{draft.recipientEmail}</strong>? Transfers are final.
          </p>
          <ConfirmButton onConfirm={() => send(draft)}>Confirm transfer</ConfirmButton>
          <button type="button" className="link" disabled={sending} onClick={() => setDraft(null)}>
            Cancel
          </button>
        </div>
      )}
      {message === null ? null : <p role="status">{message}</p>}
    </section>
  );
}
