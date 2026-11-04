import type { ReactNode } from "react";
import { formatMinor } from "../../lib/money";
import type { TransferHistoryItem } from "./types";

export function HistoryPanel(props: { items: TransferHistoryItem[] | null; error: string | null }): ReactNode {
  return (
    <section aria-labelledby="history-title">
      <h2 id="history-title">History</h2>
      {props.error === null ? null : <p role="alert">{props.error}</p>}
      {props.items === null ? null : props.items.length === 0 ? (
        <p>No transfers yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Direction</th>
              <th scope="col">Counterparty</th>
              <th scope="col" className="num">
                Amount
              </th>
            </tr>
          </thead>
          <tbody>
            {props.items.map((t) => (
              <tr key={t.transfer_id} data-testid="history-row">
                <td>{new Date(t.created_at).toLocaleString()}</td>
                <td>{t.direction === "sent" ? "Sent" : "Received"}</td>
                <td>{t.counterparty_email_masked}</td>
                <td className="num">
                  {t.direction === "sent" ? "−" : "+"}
                  {formatMinor(t.amount_minor, t.currency)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
