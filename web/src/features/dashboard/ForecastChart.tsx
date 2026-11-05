import type { ReactNode } from "react";
import { formatAmount } from "./format";
import type { CurrencyForecast } from "./types";

const WIDTH = 480;
const HEIGHT = 160;
const PAD = 8;

function point(index: number, value: number, count: number, max: number): string {
  const x = PAD + (count <= 1 ? 0 : (index / (count - 1)) * (WIDTH - 2 * PAD));
  const y = HEIGHT - PAD - (max === 0 ? 0 : (value / max) * (HEIGHT - 2 * PAD));
  return `${x.toFixed(1)},${y.toFixed(1)}`;
}

/**
 * History as a solid line, the forecast as a dashed line inside its 95% band, so the two series differ by
 * stroke style as well as by colour. The table next to it carries the same numbers for screen readers.
 */
export function ForecastChart(props: { item: CurrencyForecast; horizonDays: number }): ReactNode {
  const { item, horizonDays } = props;
  const total = item.forecast.reduce((sum, p) => sum + p.expected_minor, 0);
  const count = item.history.length + item.forecast.length;
  const max = Math.max(
    0,
    ...item.history.map((p) => p.amount_minor),
    ...item.forecast.map((p) => p.high_minor),
  );
  const offset = item.history.length;
  const history = item.history.map((p, i) => point(i, p.amount_minor, count, max)).join(" ");
  const lastHistory = item.history.at(-1);
  const forecastStart = lastHistory === undefined ? [] : [point(offset - 1, lastHistory.amount_minor, count, max)];
  const forecast = [
    ...forecastStart,
    ...item.forecast.map((p, i) => point(offset + i, p.expected_minor, count, max)),
  ].join(" ");
  const band = [
    ...item.forecast.map((p, i) => point(offset + i, p.high_minor, count, max)),
    ...item.forecast.map((p, i) => point(offset + i, p.low_minor, count, max)).reverse(),
  ].join(" ");
  const label = `${item.currency} expected payouts, next ${horizonDays} days: ${formatAmount(total, item.currency)} in total`;

  return (
    <svg className="chart" role="img" aria-label={label} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none">
      {band === "" ? null : <polygon className="chart-band" points={band} />}
      {history === "" ? null : <polyline className="chart-history" points={history} />}
      {forecast === "" ? null : <polyline className="chart-forecast" points={forecast} />}
    </svg>
  );
}
