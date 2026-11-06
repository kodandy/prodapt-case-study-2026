import { useCallback, useMemo, useState, type ReactNode } from "react";
import { useAuthState, useServices } from "../../app/services";
import { usePagedList } from "../../app/usePagedList";
import { Disclosure } from "../../components/Disclosure";
import { AssistantPanel } from "./AssistantPanel";
import { DisputesPanel } from "./DisputesPanel";
import { KeyEntry } from "./KeyEntry";
import { MerchantNotificationsPanel } from "./MerchantNotificationsPanel";
import { PaymentDetail } from "./PaymentDetail";
import { PaymentsPanel } from "./PaymentsPanel";
import { SettlementPanel } from "./SettlementPanel";
import type { PaymentList, PaymentSummary } from "./types";

const PAGE_SIZE = 20;

function paymentIdOf(payment: PaymentSummary): string {
  return payment.payment_id;
}

export function MerchantArea(): ReactNode {
  const { merchantApiKey, merchantOpsToken } = useAuthState();
  const signedIn = merchantApiKey !== null && merchantOpsToken !== null && merchantOpsToken !== "";
  return signedIn ? <SignedInMerchant /> : <KeyEntry />;
}

/** Unmounted on sign-out, so one merchant's payments never render for the next. */
function SignedInMerchant(): ReactNode {
  const { client, store } = useServices();
  const [stateFilter, setStateFilter] = useState("");
  // Bumped on every filter choice, so choosing a filter always restarts the list from its first page.
  const [filterRun, setFilterRun] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);

  const fetchPage = useCallback(
    (after: string | null) =>
      client.request<PaymentList>({
        base: "gw",
        path: "/v1/payments",
        auth: "merchantKey",
        query: { limit: PAGE_SIZE, state: stateFilter === "" ? undefined : stateFilter, cursor: after ?? undefined },
      }),
    [client, stateFilter, filterRun],
  );
  const list = usePagedList(fetchPage, paymentIdOf);
  const payments = list.items;

  const knownPaymentIds = useMemo(() => new Set((payments ?? []).map((p) => p.payment_id)), [payments]);
  const open = payments?.find((p) => p.payment_id === openId) ?? null;

  return (
    <div className="stack">
      <div className="toolbar">
        <button type="button" className="link" onClick={() => store.clearMerchant()}>
          Sign out
        </button>
      </div>
      <div className="columns">
        <div className="stack">
          <PaymentsPanel
            payments={payments}
            error={list.error}
            pending={list.pending}
            onRetry={list.retry}
            stateFilter={stateFilter}
            onFilter={(s) => {
              setStateFilter(s);
              setFilterRun((n) => n + 1);
            }}
            onOpen={setOpenId}
            hasMore={list.hasMore}
            onMore={list.loadMore}
          />
          {open === null ? null : (
            <PaymentDetail
              key={open.payment_id}
              payment={open}
              onClose={() => setOpenId(null)}
              onRefunded={list.reload}
            />
          )}
        </div>
        <div className="stack">
          <AssistantPanel knownPaymentIds={knownPaymentIds} onOpenPayment={setOpenId} />
          <SettlementPanel />
          <Disclosure label="disputes">
            <DisputesPanel />
          </Disclosure>
          <Disclosure label="notifications">
            <MerchantNotificationsPanel />
          </Disclosure>
        </div>
      </div>
    </div>
  );
}
