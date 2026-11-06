import { useState, type FormEvent, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { formatMinor } from "../../lib/money";
import type { SettlementExplanation } from "./types";
import { TableScroll } from "../../components/TableScroll";

const PERIOD = /^[0-9]{4}-(0[1-9]|1[0-2])$/;

/** The structured statement fields are primary; the narrative text is secondary. */
export function SettlementPanel(): ReactNode {
  const { client } = useServices();
  const [period, setPeriod] = useState("");
  const [result, setResult] = useState<SettlementExplanation | null>(null);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (pending) return;
    if (period !== "" && !PERIOD.test(period)) {
      setMessage("Use YYYY-MM, or leave empty for the latest statement.");
      return;
    }
    setPending(true);
    setMessage(null);
    const response = await client.request<SettlementExplanation>({
      base: "ops",
      path: "/v1/assistant/explain-settlement",
      method: "POST",
      auth: "merchantOps",
      body:
        period === ""
          ? { question: "Explain my latest payout." }
          : { question: `Explain my ${period} payout.`, period },
    });
    setPending(false);
    if (response.ok) setResult(response.data);
    else setMessage(errorMessageOf(response));
  }

  return (
    <section aria-labelledby="settlement-title">
      <h2 id="settlement-title">Settlement summary</h2>
      <form onSubmit={(e) => void submit(e)}>
        <label>
          Period (YYYY-MM)
          <input value={period} placeholder="latest" onChange={(e) => setPeriod(e.target.value)} />
        </label>
        <button type="submit" disabled={pending}>
          Show
        </button>
      </form>
      {message === null ? null : <p role="alert">{message}</p>}
      {result === null ? null : (
        <div data-testid="settlement">
          <dl className="facts">
            <dt>Period</dt>
            <dd>{result.period}</dd>
            <dt>Gross</dt>
            <dd>{formatMinor(result.gross_minor, result.currency)}</dd>
            <dt>Expected payout</dt>
            <dd>{formatMinor(result.expected_minor, result.currency)}</dd>
            <dt>Net payout</dt>
            <dd>{formatMinor(result.net_minor, result.currency)}</dd>
            <dt>Gap</dt>
            <dd>{formatMinor(result.gap_minor, result.currency)}</dd>
            <dt>Cross-check</dt>
            <dd>{result.verified ? "Verified" : `Mismatch: ${result.mismatches.join("; ")}`}</dd>
            <dt>Contract</dt>
            <dd>
              <code>{result.contract_version}</code>
            </dd>
            <dt>FX rate</dt>
            <dd>
              <code>{result.fx_rate_id}</code>
            </dd>
          </dl>
          <TableScroll label="Settlement lines table">
            <table>
              <thead>
                <tr>
                  <th scope="col">Line</th>
                  <th scope="col" className="num">
                    Count
                  </th>
                  <th scope="col" className="num">
                    Amount
                  </th>
                </tr>
              </thead>
              <tbody>
                {result.lines.map((line) => (
                  <tr key={line.kind}>
                    <td>{line.kind}</td>
                    <td className="num">{line.count}</td>
                    <td className="num">{formatMinor(line.amount_minor, result.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
          <p>{result.summary}</p>
          {result.text === null ? null : <p className="muted answer-text">{result.text}</p>}
        </div>
      )}
    </section>
  );
}
