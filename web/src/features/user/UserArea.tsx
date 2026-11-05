import { useCallback, useEffect, useState, type ReactNode } from "react";
import { errorMessageOf, useAuthState, useServices } from "../../app/services";
import { Disclosure } from "../../components/Disclosure";
import { AuthPanel } from "./AuthPanel";
import { HistoryPanel } from "./HistoryPanel";
import { PayMerchantPanel } from "./PayMerchantPanel";
import { PaymentsHistoryPanel } from "./PaymentsHistoryPanel";
import { TotpPanel } from "./TotpPanel";
import { TransferPanel } from "./TransferPanel";
import { UserNotificationsPanel } from "./UserNotificationsPanel";
import type { TransferHistory, TransferHistoryItem, WalletBalance, WalletList } from "./types";
import { WalletsPanel } from "./WalletsPanel";

export function UserArea(): ReactNode {
  const { store } = useServices();
  const { userSessionToken } = useAuthState();
  const [notice, setNotice] = useState<string | null>(null);

  if (userSessionToken === null) return <AuthPanel notice={notice} />;
  return (
    <SignedInUser
      onMfaEnabled={() => {
        setNotice("Two-factor authentication is on. Log in again with your code.");
        store.clear();
      }}
    />
  );
}

/** Unmounted on log-out, so one account's balances and history never render for the next. */
function SignedInUser(props: { onMfaEnabled: () => void }): ReactNode {
  const { client, store } = useServices();
  const [wallets, setWallets] = useState<WalletBalance[] | null>(null);
  const [walletError, setWalletError] = useState<string | null>(null);
  const [history, setHistory] = useState<TransferHistoryItem[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [paymentsVersion, setPaymentsVersion] = useState(0);

  const refresh = useCallback(async (): Promise<void> => {
    const [w, h] = await Promise.all([
      client.request<WalletList>({ base: "gw", path: "/v1/me/wallets", auth: "user" }),
      client.request<TransferHistory>({ base: "gw", path: "/v1/me/transfers", auth: "user", query: { limit: 20 } }),
    ]);
    setWallets(w.ok ? w.data.items : null);
    setWalletError(errorMessageOf(w));
    setHistory(h.ok ? h.data.items : null);
    setHistoryError(errorMessageOf(h));
  }, [client]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="stack">
      <div className="toolbar">
        <button type="button" className="link" onClick={() => store.clear()}>
          Log out
        </button>
      </div>
      <WalletsPanel wallets={wallets} error={walletError} />
      <TransferPanel onTransferred={() => void refresh()} />
      <HistoryPanel items={history} error={historyError} />
      <Disclosure label="pay a merchant">
        <PayMerchantPanel onPaid={() => setPaymentsVersion((v) => v + 1)} />
      </Disclosure>
      <Disclosure label="card payments">
        <PaymentsHistoryPanel key={paymentsVersion} />
      </Disclosure>
      <Disclosure label="notifications">
        <UserNotificationsPanel />
      </Disclosure>
      <TotpPanel onEnabled={props.onMfaEnabled} />
    </div>
  );
}
