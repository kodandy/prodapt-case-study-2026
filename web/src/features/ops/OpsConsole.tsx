import type { ReactNode } from "react";
import { useAuthState, useServices } from "../../app/services";
import { AlertsPanel } from "../dashboard/AlertsPanel";
import { KeyEntry } from "../merchant/KeyEntry";
import { FailureQueryPanel } from "./FailureQueryPanel";
import { FlaggedPanel } from "./FlaggedPanel";
import { TicketQueuePanel } from "./TicketQueuePanel";

/**
 * B13.3 ops console. There is no ops principal yet, so it signs in with the merchant token and every panel
 * calls the merchant-scoped routes; the /v1/ops/analytics/* routes answer 403 to that token by design.
 */
export function OpsConsole(): ReactNode {
  const { merchantApiKey, merchantOpsToken } = useAuthState();
  const { store } = useServices();
  const signedIn = merchantApiKey !== null && merchantOpsToken !== null && merchantOpsToken !== "";
  if (!signedIn) {
    return (
      <div className="stack">
        <p className="hint">The ops console signs in with a merchant API key until ops accounts exist.</p>
        <KeyEntry />
      </div>
    );
  }
  return (
    <div className="stack">
      <div className="toolbar">
        <button type="button" className="link" onClick={() => store.clearMerchant()}>
          Sign out
        </button>
      </div>
      <p role="note" className="banner">
        Demo: signed in with a merchant token, which has no ops role. Every panel shows this merchant&apos;s data
        only; the cross-merchant ops routes stay closed until ops roles are wired in.
      </p>
      <div className="columns">
        <FailureQueryPanel />
        <FlaggedPanel />
      </div>
      <div className="columns">
        <AlertsPanel />
        <TicketQueuePanel />
      </div>
    </div>
  );
}
