import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { errorMessageOf, useAuthState, useServices } from "../../app/services";
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

export function MerchantArea(): ReactNode {
  const { merchantApiKey, merchantOpsToken } = useAuthState();
  const signedIn = merchantApiKey !== null && merchantOpsToken !== null && merchantOpsToken !== "";
  return signedIn ? <SignedInMerchant /> : <KeyEntry />;
}

/** Unmounted on sign-out, so one merchant's payments never render for the next. */
function SignedInMerchant(): ReactNode {
  const { client, store } = useServices();
  const [stateFilter, setStateFilter] = useState("");
  const [payments, setPayments] = useState<PaymentSummary[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(
    async (after: string | null): Promise<void> => {
      const result = await client.request<PaymentList>({
        base: "gw",
        path: "/v1/payments",
        auth: "merchantKey",
        query: { limit: PAGE_SIZE, state: stateFilter === "" ? undefined : stateFilter, cursor: after ?? undefined },
      });
      setError(errorMessageOf(result));
      if (!result.ok) return;
      setPayments((prev) => (after === null || prev === null ? result.data.items : [...prev, ...result.data.items]));
      setCursor(result.data.next_cursor);
    },
    [client, stateFilter],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

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
            error={error}
            stateFilter={stateFilter}
            onFilter={(s) => {
              setPayments(null);
              setStateFilter(s);
            }}
            onOpen={setOpenId}
            hasMore={cursor !== null}
            onMore={() => void load(cursor)}
          />
          {open === null ? null : (
            <PaymentDetail
              key={open.payment_id}
              payment={open}
              onClose={() => setOpenId(null)}
              onRefunded={() => void load(null)}
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
