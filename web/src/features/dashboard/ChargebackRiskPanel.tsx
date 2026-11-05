import { useEffect, useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { formatAmount, formatPercent, formatUtcMinute } from "./format";
import type { ChargebackModel, ChargebackRiskResponse, RiskItem } from "./types";

const RISK_LIMIT = 20;

const FEATURE_LABELS: ReadonlyMap<string, string> = new Map([
  ["log_amount_usd", "Amount"],
  ["cross_border", "Cross-border"],
  ["prior_decline_24h", "Recent decline on card"],
  ["merchant_cb_rate", "Merchant chargeback history"],
]);

/** The feature that pushes the score up the most; ties keep the first feature in response order. */
function topFactor(contributions: Record<string, number>): string | null {
  let best: [string, number] | null = null;
  for (const entry of Object.entries(contributions)) {
    if (best === null || entry[1] > best[1]) best = entry;
  }
  return best === null ? null : (FEATURE_LABELS.get(best[0]) ?? best[0]);
}

function modelLine(model: ChargebackModel): string {
  const auc = model.auc === null ? "n/a" : model.auc.toFixed(2);
  return `Model AUC ${auc} · Brier ${model.brier.toFixed(4)} (base rate ${model.base_rate_brier.toFixed(4)})`;
}

function RiskRow(props: { item: RiskItem }): ReactNode {
  const { item } = props;
  return (
    <tr data-testid="risk-row" data-payment-id={item.payment_id}>
      <td>
        <code>{item.payment_id}</code>
      </td>
      <td>{formatUtcMinute(item.occurred_at)}</td>
      <td className="num">{formatAmount(item.amount_minor, item.currency)}</td>
      <td className="num">{formatPercent(item.probability)}</td>
      <td>{topFactor(item.contributions) ?? "—"}</td>
    </tr>
  );
}

export function ChargebackRiskPanel(): ReactNode {
  const { client } = useServices();
  const [data, setData] = useState<ChargebackRiskResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void client
      .request<ChargebackRiskResponse>({
        base: "ops",
        path: "/v1/analytics/chargeback-risk",
        auth: "merchantOps",
        query: { limit: RISK_LIMIT },
      })
      .then((result) => {
        if (!active) return;
        setError(errorMessageOf(result));
        if (result.ok) setData(result.data);
      });
    return () => {
      active = false;
    };
  }, [client]);

  return (
    <section aria-labelledby="risk-title">
      <h2 id="risk-title">Chargeback risk</h2>
      {error === null ? null : <p role="alert">{error}</p>}
      {data === null ? null : (
        <>
          <p>{`Merchant chargeback rate ${formatPercent(data.merchant_chargeback_rate)}`}</p>
          <p className="hint">{modelLine(data.model)}</p>
          <p className="hint">{`Scores come from a model trained on synthetic labels (${data.model.trained_on}); treat them as a ranking, not a verdict.`}</p>
          {data.items.length === 0 ? (
            <p className="muted">No recent captured payments to score.</p>
          ) : (
            <table>
              <caption className="sr-only">Payments ranked by chargeback probability</caption>
              <thead>
                <tr>
                  <th scope="col">Payment</th>
                  <th scope="col">Time</th>
                  <th scope="col" className="num">
                    Amount
                  </th>
                  <th scope="col" className="num">
                    Probability
                  </th>
                  <th scope="col">Top factor</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <RiskRow key={item.payment_id} item={item} />
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
