import type { ReactNode } from "react";
import { useAuthState, useServices } from "../../app/services";
import { KeyEntry } from "../merchant/KeyEntry";
import { AlertsPanel } from "./AlertsPanel";
import { ChargebackRiskPanel } from "./ChargebackRiskPanel";
import { ForecastPanel } from "./ForecastPanel";

/** The panels mount only while signed in, so one merchant's analytics never render for the next. */
export function DashboardArea(): ReactNode {
  const { merchantApiKey, merchantOpsToken } = useAuthState();
  const { store } = useServices();
  const signedIn = merchantApiKey !== null && merchantOpsToken !== null && merchantOpsToken !== "";
  if (!signedIn) return <KeyEntry />;
  return (
    <div className="stack">
      <div className="toolbar">
        <button type="button" className="link" onClick={() => store.clearMerchant()}>
          Sign out
        </button>
      </div>
      <ForecastPanel />
      <div className="columns">
        <ChargebackRiskPanel />
        <AlertsPanel />
      </div>
    </div>
  );
}
