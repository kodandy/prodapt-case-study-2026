import type { ReactNode } from "react";
import { TableScroll } from "../../components/TableScroll";
import { formatMinor } from "../../lib/money";
import { PAYMENT_STATE_FILTERS, type PaymentSummary } from "./types";

export function PaymentsPanel(props: {
  payments: PaymentSummary[] | null;
  error: string | null;
  pending: boolean;
  onRetry: () => void;
  stateFilter: string;
  onFilter: (state: string) => void;
  onOpen: (paymentId: string) => void;
  hasMore: boolean;
  onMore: () => void;
}): ReactNode {
  return (
    <section aria-labelledby="payments-title">
      <h2 id="payments-title">Payments</h2>
      <label>
        State
        <select value={props.stateFilter} onChange={(e) => props.onFilter(e.target.value)}>
          <option value="">All</option>
          {PAYMENT_STATE_FILTERS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
      </label>
      {props.error === null ? null : (
        <div className="row-actions">
          <p role="alert">{props.error}</p>
          <button type="button" onClick={props.onRetry}>
            Retry
          </button>
        </div>
      )}
      {props.payments === null ? (
        props.error === null ? <p>Loading…</p> : null
      ) : props.payments.length === 0 ? (
        <p>No payments.</p>
      ) : (
        <TableScroll label="Payments table">
          <table>
            <thead>
              <tr>
                <th scope="col">Created</th>
                <th scope="col">State</th>
                <th scope="col">Card</th>
                <th scope="col" className="num">
                  Amount
                </th>
                <th scope="col">
                  <span className="sr-only">Open</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {props.payments.map((p) => (
                <tr key={p.payment_id} data-testid="payment-row" data-payment-id={p.payment_id}>
                  <td>{new Date(p.created_at).toLocaleString()}</td>
                  <td>
                    {p.state}
                    {p.decline_code === null ? null : <span className="muted"> ({p.decline_code})</span>}
                  </td>
                  <td>{p.card === null ? "—" : `${p.card.bin} •••• ${p.card.last4}`}</td>
                  <td className="num">{formatMinor(p.amount_minor, p.currency)}</td>
                  <td>
                    <button type="button" className="link" onClick={() => props.onOpen(p.payment_id)}>
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
      {props.hasMore ? (
        <button type="button" className="link" disabled={props.pending} onClick={props.onMore}>
          Load more
        </button>
      ) : null}
    </section>
  );
}
