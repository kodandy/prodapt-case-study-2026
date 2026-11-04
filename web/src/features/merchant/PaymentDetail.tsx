import { useEffect, useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { ConfirmButton } from "../../components/ConfirmButton";
import { isOutcomeUnknown } from "../../lib/outcome";
import { formatMinor, parseToMinor } from "../../lib/money";
import { refundableMinor, type PaymentRisk, type PaymentSummary, type RefundResult } from "./types";

interface RefundDraft {
  amountMinor: number;
  idempotencyKey: string;
}

/** The only place a refund starts: explicit amount review, then a confirm step (B13.2). */
export function PaymentDetail(props: {
  payment: PaymentSummary;
  onClose: () => void;
  onRefunded: () => void;
}): ReactNode {
  const { client, pendingWrites } = useServices();
  const { payment } = props;
  const refundable = refundableMinor(payment);
  const [risk, setRisk] = useState<PaymentRisk | null>(null);
  const [amountText, setAmountText] = useState("");
  const [draft, setDraft] = useState<RefundDraft | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const scope = `merchant:refund:${payment.payment_id}`;

  function draftFor(amountMinor: number): RefundDraft {
    return { amountMinor, idempotencyKey: pendingWrites.keyFor(scope, { amount_minor: amountMinor }) };
  }

  useEffect(() => {
    let live = true;
    void client
      .request<PaymentRisk>({ base: "gw", path: `/v1/payments/${payment.payment_id}/risk`, auth: "merchantKey" })
      .then((r) => {
        if (live && r.ok) setRisk(r.data);
      });
    return () => {
      live = false;
    };
  }, [client, payment.payment_id]);

  function review(): void {
    if (amountText.trim() === "") {
      setDraft(draftFor(refundable));
      setMessage(null);
      return;
    }
    const parsed = parseToMinor(amountText, payment.currency);
    if (!parsed.ok) {
      setMessage("Enter a valid amount, or leave it empty for the full refundable amount.");
      return;
    }
    if (parsed.amountMinor > refundable) {
      setMessage(`At most ${formatMinor(refundable, payment.currency)} can be refunded.`);
      return;
    }
    setMessage(null);
    setDraft(draftFor(parsed.amountMinor));
  }

  async function refund(current: RefundDraft): Promise<void> {
    setSending(true);
    const result = await client.request<RefundResult>({
      base: "gw",
      path: `/v1/payments/${payment.payment_id}/refunds`,
      method: "POST",
      auth: "merchantKey",
      // Explicit even for a full refund, so the server never refunds more than the merchant confirmed.
      body: { amount_minor: current.amountMinor },
      idempotencyKey: current.idempotencyKey,
      retries: 1,
    });
    setSending(false);
    if (isOutcomeUnknown(result)) {
      setMessage(`${errorMessageOf(result)} Press Confirm again to retry the same refund.`);
      return;
    }
    pendingWrites.settle(scope, { amount_minor: current.amountMinor }, current.idempotencyKey);
    setDraft(null);
    if (result.ok) {
      setAmountText("");
      setMessage(`Refunded ${formatMinor(result.data.amount_minor, result.data.currency)}.`);
      props.onRefunded();
    } else {
      setMessage(errorMessageOf(result));
    }
  }

  return (
    <section aria-labelledby="payment-title" className="detail">
      <h3 id="payment-title">
        Payment <code>{payment.payment_id}</code>
      </h3>
      <dl className="facts">
        <dt>State</dt>
        <dd data-testid="payment-state">{payment.state}</dd>
        <dt>Amount</dt>
        <dd>{formatMinor(payment.amount_minor, payment.currency)}</dd>
        <dt>Refunded</dt>
        <dd>{formatMinor(payment.refunded_minor, payment.currency)}</dd>
        <dt>Refundable</dt>
        <dd data-testid="refundable">{formatMinor(refundable, payment.currency)}</dd>
        <dt>Card</dt>
        <dd>{payment.card === null ? "—" : `${payment.card.bin} •••• ${payment.card.last4}`}</dd>
        <dt>Risk</dt>
        <dd>{risk === null ? "…" : risk.blocked ? "Blocked by risk rules" : risk.risk_flagged ? "Flagged" : "Clear"}</dd>
      </dl>
      {refundable > 0 ? (
        draft === null ? (
          <div className="form">
            <label>
              Refund amount (empty = full)
              <input inputMode="decimal" value={amountText} onChange={(e) => setAmountText(e.target.value)} />
            </label>
            <button type="button" onClick={review}>
              Review refund
            </button>
          </div>
        ) : (
          <div className="confirm" role="group" aria-label="Confirm refund">
            <p>
              Refund <strong>{formatMinor(draft.amountMinor, payment.currency)}</strong> of{" "}
              {formatMinor(refundable, payment.currency)} refundable?
            </p>
            <ConfirmButton onConfirm={() => refund(draft)}>Confirm refund</ConfirmButton>
            <button type="button" className="link" disabled={sending} onClick={() => setDraft(null)}>
              Cancel
            </button>
          </div>
        )
      ) : (
        <p>Nothing left to refund.</p>
      )}
      {message === null ? null : <p role="status">{message}</p>}
      <button type="button" className="link" disabled={sending} onClick={props.onClose}>
        Close
      </button>
    </section>
  );
}
