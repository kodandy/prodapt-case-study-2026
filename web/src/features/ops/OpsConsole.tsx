import type { ReactNode } from "react";
import { useAuthState, useServices } from "../../app/services";
import { AlertsPanel } from "../dashboard/AlertsPanel";
import { KeyEntry } from "../merchant/KeyEntry";
import { FailureQueryPanel } from "./FailureQueryPanel";
import { FlaggedPanel } from "./FlaggedPanel";
import { OpsPrincipalConsole } from "./OpsPrincipalConsole";
import { OpsSignIn } from "./OpsSignIn";
import { TicketQueuePanel } from "./TicketQueuePanel";
import { useOpsSessionState, useOptionalOpsServices } from "./opsServices";

/**
 * B13.3 ops console. Ops staff sign in with password + TOTP (gateway ops session) and get the cross-merchant
 * view on /v1/ops/analytics/*. The merchant-token sign-in stays as a merchant-scoped demo: those panels call the
 * merchant routes, and the ops routes answer 403 to that token by design.
 */
export function OpsConsole(): ReactNode {
  const { merchantApiKey, merchantOpsToken } = useAuthState();
  const { store } = useServices();
  const opsServices = useOptionalOpsServices();
  const ops = useOpsSessionState(opsServices);
  if (opsServices !== null && ops.sessionToken !== null && ops.opsAiToken !== null) {
    return <OpsPrincipalConsole role={ops.role ?? "unknown"} />;
  }
  const signedIn = merchantApiKey !== null && merchantOpsToken !== null && merchantOpsToken !== "";
  if (!signedIn) {
    return (
      <div className="stack">
        {opsServices === null ? null : <OpsSignIn />}
        <p className="hint">
          Without an ops account, a merchant API key opens a merchant-scoped demo of the ops panels (one
          merchant&apos;s data only).
        </p>
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
        only; sign out and use the ops staff sign-in for the cross-merchant view.
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
