import { useCallback, useEffect, useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { CURRENCIES, formatMinor } from "../../lib/money";
import type { ConsumerPayment, ConsumerPaymentList } from "./types";

const PAGE_SIZE = 20;
const STATUS_OPTIONS = ["succeeded", "declined", "failed"] as const;
const DISPUTE_REASONS = [
  { value: "fraudulent", label: "I did not make this payment" },
  { value: "product_not_received", label: "I did not receive the product" },
  { value: "duplicate", label: "I was charged twice" },
  { value: "other", label: "Other" },
] as const;
const DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

const ERROR_MESSAGES: ReadonlyMap<string, string> = new Map([
  ["refund_already_requested", "A refund was already requested for this payment."],
  ["refund_exceeds_refundable", "Nothing is left to refund on this payment."],
  ["payment not disputable", "This payment cannot be disputed."],
  ["dispute already open", "A dispute is already open for this payment."],
  ["dispute amount exceeds", "Nothing is left to dispute on this payment."],
  ["payment not found", "Payment not found."],
]);

interface Filters {
  state: string;
  from: string;
  to: string;
}

type Feedback = { kind: "status" | "alert"; text: string } | null;

/** "YYYY-MM-DD" → that day's UTC midnight, `dayOffset` days later; null for anything else. */
function utcMidnight(date: string, dayOffset: number): string | null {
  const match = DATE.exec(date);
  if (match === null) return null;
  const at = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]) + dayOffset));
  if (Number.isNaN(at.getTime())) return null;
  return `${at.toISOString().slice(0, 10)}T00:00:00Z`;
}

function amountText(payment: ConsumerPayment): string {
  return (CURRENCIES as readonly string[]).includes(payment.currency)
    ? formatMinor(payment.amount_minor, payment.currency)
    : `${payment.amount_minor} ${payment.currency}`;
}

/** B13.1: payments the user made, with filters, paging, a refund request and a dispute per succeeded payment. */
export function PaymentsHistoryPanel(): ReactNode {
  const { client } = useServices();
  const [draftFilters, setDraftFilters] = useState<Filters>({ state: "", from: "", to: "" });
  const [applied, setApplied] = useState<Filters>({ state: "", from: "", to: "" });
  const [items, setItems] = useState<ConsumerPayment[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [disputing, setDisputing] = useState<string | null>(null);
  const [reason, setReason] = useState<string>(DISPUTE_REASONS[0].value);
  const [sending, setSending] = useState(false);

  const load = useCallback(
    async (after: string | null): Promise<void> => {
      const result = await client.request<ConsumerPaymentList>({
        base: "gw",
        path: "/v1/me/payments",
        auth: "user",
        query: {
          limit: PAGE_SIZE,
          cursor: after ?? undefined,
          state: applied.state === "" ? undefined : applied.state,
          created_from: utcMidnight(applied.from, 0) ?? undefined,
          created_to: utcMidnight(applied.to, 1) ?? undefined,
        },
      });
      setLoadError(errorMessageOf(result));
      if (!result.ok) return;
      setItems((prev) => (after === null || prev === null ? result.data.items : [...prev, ...result.data.items]));
      setCursor(result.data.next_cursor);
    },
    [client, applied],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  function applyFilters(): void {
    if (draftFilters.from !== "" && draftFilters.to !== "" && draftFilters.from > draftFilters.to) {
      setFeedback({ kind: "alert", text: "From must not be after To." });
      return;
    }
    setFeedback(null);
    setDisputing(null);
    setApplied({ ...draftFilters });
  }

  async function requestRefund(payment: ConsumerPayment): Promise<void> {
    setSending(true);
    const result = await client.request<{ payment_id: string; requested_minor: number }>({
      base: "gw",
      path: `/v1/me/payments/${encodeURIComponent(payment.payment_id)}/refund-requests`,
      method: "POST",
      auth: "user",
      body: {},
    });
    setSending(false);
    setFeedback(
      result.ok
        ? { kind: "status", text: "Refund requested." }
        : { kind: "alert", text: ERROR_MESSAGES.get(result.code) ?? errorMessageOf(result) ?? "" },
    );
  }

  async function openDispute(paymentId: string): Promise<void> {
    setSending(true);
    const result = await client.request<unknown>({
      base: "gw",
      path: `/v1/me/payments/${encodeURIComponent(paymentId)}/disputes`,
      method: "POST",
      auth: "user",
      body: { reason },
    });
    setSending(false);
    if (result.ok) {
      setDisputing(null);
      setFeedback({ kind: "status", text: "Dispute opened." });
      return;
    }
    setFeedback({ kind: "alert", text: ERROR_MESSAGES.get(result.code) ?? errorMessageOf(result) ?? "" });
  }

  return (
    <section aria-labelledby="payments-history-title">
      <h2 id="payments-history-title">Payments</h2>
      <div className="form inline">
        <label>
          Status
          <select
            value={draftFilters.state}
            onChange={(e) => setDraftFilters((f) => ({ ...f, state: e.target.value }))}
          >
            <option value="">All</option>
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label>
          From
          <input
            type="date"
            value={draftFilters.from}
            onChange={(e) => setDraftFilters((f) => ({ ...f, from: e.target.value }))}
          />
        </label>
        <label>
          To
          <input
            type="date"
            value={draftFilters.to}
            onChange={(e) => setDraftFilters((f) => ({ ...f, to: e.target.value }))}
          />
        </label>
        <button type="button" onClick={applyFilters}>
          Apply filters
        </button>
      </div>
      {loadError === null ? null : <p role="alert">{loadError}</p>}
      {items === null ? null : items.length === 0 ? (
        <p className="muted">No payments.</p>
      ) : (
        <ul className="list">
          {items.map((payment) => (
            <li key={payment.payment_id}>
              <span>{amountText(payment)}</span> <span className="muted">{payment.state}</span>
              {payment.card === null ? null : <span className="muted"> card ending {payment.card.last4}</span>}
              <span className="muted"> {new Date(payment.created_at).toLocaleString()}</span>
              {payment.chargeback_flag ? (
                <>
                  {" "}
                  <span className="badge">Chargeback</span>
                </>
              ) : null}
              {payment.state === "succeeded" ? (
                <span className="actions">
                  <button type="button" disabled={sending} onClick={() => void requestRefund(payment)}>
                    Request refund
                  </button>
                  <button
                    type="button"
                    disabled={sending}
                    onClick={() => {
                      setReason(DISPUTE_REASONS[0].value);
                      setDisputing(payment.payment_id);
                    }}
                  >
                    Open dispute
                  </button>
                </span>
              ) : null}
              {disputing === payment.payment_id ? (
                <div className="form">
                  <label>
                    Reason
                    <select value={reason} onChange={(e) => setReason(e.target.value)}>
                      {DISPUTE_REASONS.map((r) => (
                        <option key={r.value} value={r.value}>
                          {r.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button type="button" disabled={sending} onClick={() => void openDispute(payment.payment_id)}>
                    Submit dispute
                  </button>
                  <button type="button" className="link" disabled={sending} onClick={() => setDisputing(null)}>
                    Cancel
                  </button>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {cursor === null ? null : (
        <button type="button" onClick={() => void load(cursor)}>
          Load more
        </button>
      )}
      {feedback === null ? null : <p role={feedback.kind}>{feedback.text}</p>}
    </section>
  );
}
