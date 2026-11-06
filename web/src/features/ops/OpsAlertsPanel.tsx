import { useEffect, useState, type ReactNode } from "react";
import { messageFor } from "../../lib/errors";
import { AlertItem, mergeAlerts } from "../dashboard/AlertsPanel";
import { useOpsServices } from "./opsServices";
import type { OpsAlertsResponse, OpsAnomalyAlert } from "./types";

const DEFAULT_POLL_MS = 15_000;
const ALERTS_LIMIT = 50;

/** Every merchant's anomaly alerts via the ops route; polls with since = the previous server_time. */
export function OpsAlertsPanel(props: { pollMs?: number; onSelectMerchant: (merchantId: string) => void }): ReactNode {
  const pollMs = props.pollMs ?? DEFAULT_POLL_MS;
  const { onSelectMerchant } = props;
  const { client } = useOpsServices();
  const [alerts, setAlerts] = useState<OpsAnomalyAlert[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let since: string | undefined;

    async function poll(): Promise<void> {
      try {
        const result = await client.request<OpsAlertsResponse>({
          base: "ops",
          path: "/v1/ops/analytics/alerts",
          auth: "opsAi",
          query: { limit: ALERTS_LIMIT, since },
        });
        if (!active) return;
        setError(result.ok ? null : messageFor(result.code));
        if (result.ok) {
          since = result.data.server_time;
          const items = result.data.items;
          setAlerts((previous) => mergeAlerts(previous, items));
        }
      } catch {
        if (!active) return;
        setError(messageFor("network_error"));
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
    <section aria-labelledby="ops-alerts-title">
      <h2 id="ops-alerts-title">Cross-merchant anomaly alerts</h2>
      {error === null ? null : <p role="alert">{error}</p>}
      <div aria-live="polite">
        {alerts === null ? null : alerts.length === 0 ? (
          <p className="muted">No anomalies detected on any merchant.</p>
        ) : (
          <ul className="plain-list">
            {alerts.map((alert) => (
              <AlertItem
                key={alert.alert_id}
                alert={alert}
                merchantId={alert.merchant_id}
                action={
                  <>
                    {" · merchant "}
                    <code>{alert.merchant_id}</code>{" "}
                    <button
                      type="button"
                      className="link"
                      aria-label={`Analyse merchant ${alert.merchant_id}`}
                      onClick={() => onSelectMerchant(alert.merchant_id)}
                    >
                      Analyse
                    </button>
                  </>
                }
              />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
