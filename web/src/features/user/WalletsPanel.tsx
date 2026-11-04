import type { ReactNode } from "react";
import { formatMinor } from "../../lib/money";
import type { WalletBalance } from "./types";

export function WalletsPanel(props: { wallets: WalletBalance[] | null; error: string | null }): ReactNode {
  return (
    <section aria-labelledby="wallets-title">
      <h2 id="wallets-title">Wallets</h2>
      {props.error === null ? null : <p role="alert">{props.error}</p>}
      {props.wallets === null ? (
        <p>Loading…</p>
      ) : props.wallets.length === 0 ? (
        <p>No wallets yet.</p>
      ) : (
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
      )}
    </section>
  );
}
