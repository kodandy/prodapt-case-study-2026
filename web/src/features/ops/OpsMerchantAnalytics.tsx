import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { messageFor } from "../../lib/errors";
import { ChargebackRiskView } from "../dashboard/ChargebackRiskPanel";
import { ForecastView } from "../dashboard/ForecastPanel";
import type { ChargebackRiskResponse, ForecastResponse } from "../dashboard/types";
import { useOpsServices } from "./opsServices";

const HORIZON_DAYS = 7;
const RISK_LIMIT = 20;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface Loaded {
  merchantId: string;
  forecast: ForecastResponse | null;
  risk: ChargebackRiskResponse | null;
  error: string | null;
}

/**
 * Forecast and chargeback risk for one merchant chosen by ops staff, from /v1/ops/analytics/* with an explicit
 * merchant_id. `requested` is a merchant picked elsewhere (an alert); a newer request discards older answers.
 */
export function OpsMerchantAnalytics(props: { requested: { merchantId: string; seq: number } | null }): ReactNode {
  const { client } = useOpsServices();
  const [input, setInput] = useState("");
  const [inputError, setInputError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [pending, setPending] = useState(false);
  const latest = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(
    async (merchantId: string): Promise<void> => {
      const seq = ++latest.current;
      if (!UUID.test(merchantId)) {
        setPending(false);
        setInputError("Enter the merchant's ID (a UUID).");
        return;
      }
      setInputError(null);
      setPending(true);
      let next: Loaded;
      try {
        const [forecast, risk] = await Promise.all([
          client.request<ForecastResponse>({
            base: "ops",
            path: "/v1/ops/analytics/forecast",
            auth: "opsAi",
            query: { merchant_id: merchantId, horizon_days: HORIZON_DAYS },
          }),
          client.request<ChargebackRiskResponse>({
            base: "ops",
            path: "/v1/ops/analytics/chargeback-risk",
            auth: "opsAi",
            query: { merchant_id: merchantId, limit: RISK_LIMIT },
          }),
        ]);
        const failed = !forecast.ok ? forecast : !risk.ok ? risk : null;
        next = {
          merchantId,
          forecast: forecast.ok ? forecast.data : null,
          risk: risk.ok ? risk.data : null,
          error: failed === null || failed.ok ? null : messageFor(failed.code),
        };
      } catch {
        next = { merchantId, forecast: null, risk: null, error: messageFor("network_error") };
      }
      if (!mounted.current || seq !== latest.current) return;
      setPending(false);
      setLoaded(next);
    },
    [client],
  );

  const requested = props.requested;
  useEffect(() => {
    if (requested === null) return;
    setInput(requested.merchantId);
    void load(requested.merchantId);
  }, [requested, load]);

  function submit(event: FormEvent): void {
    event.preventDefault();
    void load(input.trim());
  }

  return (
    <section aria-labelledby="ops-merchant-title">
      <h2 id="ops-merchant-title">Merchant analytics</h2>
      <form onSubmit={submit}>
        <label>
          Merchant ID
          <input autoComplete="off" spellCheck={false} value={input} onChange={(e) => setInput(e.target.value)} />
        </label>
        <button type="submit" disabled={pending}>
          Load analytics
        </button>
      </form>
      {inputError === null ? null : <p role="alert">{inputError}</p>}
      {loaded === null ? null : (
        <div className="stack-tight">
          <p>
            <strong>{`Merchant ${loaded.merchantId}`}</strong>
          </p>
          {loaded.error === null ? null : <p role="alert">{loaded.error}</p>}
          {loaded.forecast === null ? null : (
            <>
              <h3>Settlement forecast</h3>
              <ForecastView data={loaded.forecast} />
            </>
          )}
          {loaded.risk === null ? null : (
            <>
              <h3>Chargeback risk</h3>
              <ChargebackRiskView data={loaded.risk} />
            </>
          )}
        </div>
      )}
    </section>
  );
}
