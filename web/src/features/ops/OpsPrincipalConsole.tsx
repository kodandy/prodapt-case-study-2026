import { useState, type ReactNode } from "react";
import { OpsAlertsPanel } from "./OpsAlertsPanel";
import { OpsMerchantAnalytics } from "./OpsMerchantAnalytics";
import { useOpsServices } from "./opsServices";

/** B13.3 / R2-8 as the ops principal: cross-merchant alerts and per-merchant analytics on /v1/ops/analytics/*. */
export function OpsPrincipalConsole(props: { role: string }): ReactNode {
  const { store } = useOpsServices();
  const [requested, setRequested] = useState<{ merchantId: string; seq: number } | null>(null);

  function select(merchantId: string): void {
    setRequested((previous) => ({ merchantId, seq: (previous?.seq ?? 0) + 1 }));
  }

  return (
    <div className="stack">
      <div className="toolbar">
        <p className="hint">{`Signed in as ops staff (role ${props.role})`}</p>
        <button type="button" className="link" onClick={() => store.clear()}>
          Sign out
        </button>
      </div>
      <div className="columns">
        <OpsAlertsPanel onSelectMerchant={select} />
        <OpsMerchantAnalytics requested={requested} />
      </div>
    </div>
  );
}
