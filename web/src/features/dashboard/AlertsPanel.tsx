import { useEffect, useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { formatPercent, formatUtcMinute } from "./format";
import type { AlertsResponse, AnomalyAlert } from "./types";

const DEFAULT_POLL_MS = 15_000;
const ALERTS_LIMIT = 50;
export const MAX_ALERTS_SHOWN = 200;

const METRIC_LABELS: ReadonlyMap<string, string> = new Map([
  ["failure_rate", "Failure rate"],
  ["volume", "Volume"],
  ["decline_mix", "Decline mix"],
]);

function newestFirst(a: AnomalyAlert, b: AnomalyAlert): number {
  const byTime = Date.parse(b.updated_at) - Date.parse(a.updated_at);
  if (byTime !== 0) return byTime;
  return a.alert_id < b.alert_id ? -1 : a.alert_id > b.alert_id ? 1 : 0;
}

/** A later copy of an alert_id replaces the earlier one (the server upserts alerts while a bucket fills). */
function merge(previous: AnomalyAlert[] | null, incoming: AnomalyAlert[]): AnomalyAlert[] {
  const byId = new Map((previous ?? []).map((alert) => [alert.alert_id, alert]));
  for (const alert of incoming) byId.set(alert.alert_id, alert);
  return [...byId.values()].sort(newestFirst).slice(0, MAX_ALERTS_SHOWN);
}

function valueText(alert: AnomalyAlert, value: number): string {
  return alert.metric === "volume" ? String(Math.round(value)) : formatPercent(value);
}

function AlertItem(props: { alert: AnomalyAlert }): ReactNode {
  const { alert } = props;
  const label = METRIC_LABELS.get(alert.metric) ?? alert.metric;
  return (
    <li data-testid="alert-item" data-alert-id={alert.alert_id} className="alert-item">
      <strong>{label}</strong>
      {alert.detail === null ? null : (
        <>
          {" "}
          <code>{alert.detail}</code>
        </>
      )}
      <span>{` ${valueText(alert, alert.observed)} vs expected ${valueText(alert, alert.expected)} · z=${alert.zscore.toFixed(1)}`}</span>
      <span className="hint">{` · ${alert.attempts} attempts in the window from ${formatUtcMinute(alert.bucket_start)}`}</span>
    </li>
  );
}

/** Polls with since = the previous response's server_time, so each request returns only changed alerts. */
export function AlertsPanel(props: { pollMs?: number }): ReactNode {
  const pollMs = props.pollMs ?? DEFAULT_POLL_MS;
  const { client } = useServices();
  const [alerts, setAlerts] = useState<AnomalyAlert[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let since: string | undefined;

    async function poll(): Promise<void> {
      const result = await client.request<AlertsResponse>({
        base: "ops",
        path: "/v1/analytics/alerts",
        auth: "merchantOps",
        query: { limit: ALERTS_LIMIT, since },
      });
      if (!active) return;
      setError(errorMessageOf(result));
      if (result.ok) {
        since = result.data.server_time;
        const items = result.data.items;
        setAlerts((previous) => merge(previous, items));
      }
      timer = setTimeout(() => void poll(), pollMs);
    }

    void poll();
    return () => {
      active = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [client, pollMs]);

  return (
    <section aria-labelledby="alerts-title">
      <h2 id="alerts-title">Live anomaly alerts</h2>
      {error === null ? null : <p role="alert">{error}</p>}
      <div aria-live="polite">
        {alerts === null ? null : alerts.length === 0 ? (
          <p className="muted">No anomalies detected.</p>
        ) : (
          <ul className="plain-list">
            {alerts.map((alert) => (
              <AlertItem key={alert.alert_id} alert={alert} />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
