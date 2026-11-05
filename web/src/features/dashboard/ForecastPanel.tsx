import { useEffect, useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { ForecastChart } from "./ForecastChart";
import { formatAmount, formatPercent } from "./format";
import type { Backtest, CurrencyForecast, ForecastResponse } from "./types";

const HORIZON_DAYS = 7;

function backtestText(backtest: Backtest | null): string {
  if (backtest === null) return "No backtest (not enough history)";
  const wape = backtest.wape === null ? "n/a" : formatPercent(backtest.wape);
  const naive = backtest.naive_wape === null ? "n/a" : formatPercent(backtest.naive_wape);
  return `Backtest WAPE ${wape} vs naive ${naive}`;
}

function CurrencyBlock(props: { item: CurrencyForecast; horizonDays: number }): ReactNode {
  const { item, horizonDays } = props;
  return (
    <div className="stack-tight">
      <h3>{item.currency}</h3>
      <ForecastChart item={item} horizonDays={horizonDays} />
      <p className="hint">{backtestText(item.backtest)}</p>
      <table>
        <caption className="sr-only">{`${item.currency} forecast`}</caption>
        <thead>
          <tr>
            <th scope="col">Day</th>
            <th scope="col" className="num">
              Expected
            </th>
            <th scope="col" className="num">
              Low
            </th>
            <th scope="col" className="num">
              High
            </th>
          </tr>
        </thead>
        <tbody>
          {item.forecast.map((p) => (
            <tr key={p.day}>
              <td>{p.day}</td>
              <td className="num">{formatAmount(p.expected_minor, item.currency)}</td>
              <td className="num">{formatAmount(p.low_minor, item.currency)}</td>
              <td className="num">{formatAmount(p.high_minor, item.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ForecastPanel(): ReactNode {
  const { client } = useServices();
  const [data, setData] = useState<ForecastResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void client
      .request<ForecastResponse>({
        base: "ops",
        path: "/v1/analytics/forecast",
        auth: "merchantOps",
        query: { horizon_days: HORIZON_DAYS },
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
    <section aria-labelledby="forecast-title">
      <h2 id="forecast-title">Settlement forecast</h2>
      {error === null ? null : <p role="alert">{error}</p>}
      {data === null ? null : data.currencies.length === 0 ? (
        <p className="muted">{`No captured payments in the last ${data.history_days} days.`}</p>
      ) : (
        <>
          <p className="hint">
            Solid line: daily captured volume. Dashed line: expected payouts, with the shaded 95% range.
          </p>
          {data.currencies.map((item) => (
            <CurrencyBlock key={item.currency} item={item} horizonDays={data.horizon_days} />
          ))}
        </>
      )}
    </section>
  );
}
