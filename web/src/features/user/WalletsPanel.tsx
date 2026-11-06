import type { ReactNode } from "react";
import { formatMinor } from "../../lib/money";
import type { WalletBalance } from "./types";
import { TableScroll } from "../../components/TableScroll";

/** Exactly one of: the error with Retry, Loading, or the balances. */
export function WalletsPanel(props: {
  wallets: WalletBalance[] | null;
  error: string | null;
  onRetry: () => void;
}): ReactNode {
  return (
    <section aria-labelledby="wallets-title">
      <h2 id="wallets-title">Wallets</h2>
      {props.error !== null ? (
        <div className="row-actions">
          <p role="alert">{props.error}</p>
          <button type="button" onClick={props.onRetry}>
            Retry
          </button>
        </div>
      ) : props.wallets === null ? (
        <p>Loading…</p>
      ) : props.wallets.length === 0 ? (
        <p>No wallets yet.</p>
      ) : (
        <TableScroll label="Wallets table">
          <table>
            <thead>
              <tr>
                <th scope="col">Currency</th>
                <th scope="col" className="num">
                  Available
                </th>
                <th scope="col" className="num">
                  Held
                </th>
              </tr>
            </thead>
            <tbody>
              {props.wallets.map((w) => (
                <tr key={w.account_id} data-testid={`wallet-${w.currency}`}>
                  <td>{w.currency}</td>
                  <td className="num">{formatMinor(w.available_minor, w.currency)}</td>
                  <td className="num">{formatMinor(w.held_minor, w.currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </section>
  );
}
