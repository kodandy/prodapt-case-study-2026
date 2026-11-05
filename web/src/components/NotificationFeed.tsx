import { useCallback, useEffect, useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../app/services";
import type { ApiAuth } from "../lib/api";
import { CURRENCIES, formatMinor, type Currency } from "../lib/money";

export type NotificationPayloadValue = string | number | boolean | null;

export interface NotificationItem {
  notification_id: string;
  kind: string;
  payment_id: string | null;
  payload: Record<string, NotificationPayloadValue>;
  created_at: string;
  read: boolean;
  response: "confirmed" | "not_me" | null;
}

export interface NotificationPage {
  items: NotificationItem[];
  next_cursor: string | null;
}

export type Feedback = { kind: "status" | "alert"; text: string } | null;

/** What a row's extra actions may do: replace the row and report an outcome. */
export interface RowActions {
  update: (next: NotificationItem) => void;
  report: (feedback: Feedback) => void;
}

const PAGE_SIZE = 20;

const KIND_LABELS: ReadonlyMap<string, string> = new Map([
  ["payment_receipt", "Payment receipt"],
  ["was_this_you", "Was this you?"],
  ["dispute_opened", "Dispute opened"],
  ["dispute_resolved", "Dispute resolved"],
  ["dispute_update", "Dispute update"],
  ["refund_requested", "Refund requested"],
  ["payment_confirmation", "Payment confirmed"],
  ["payment_failed", "Payment failed"],
]);

/** A readable label; unknown kinds never echo the raw code. */
export function notificationLabel(kind: string): string {
  return KIND_LABELS.get(kind) ?? "Notification";
}

function amountOf(payload: Record<string, NotificationPayloadValue>): string | null {
  const amount = payload["amount_minor"] ?? payload["requested_minor"];
  const currency = payload["currency"];
  if (typeof amount !== "number" || typeof currency !== "string") return null;
  if (!(CURRENCIES as readonly string[]).includes(currency)) return null;
  return formatMinor(amount, currency as Currency);
}

/**
 * The caller's own notifications, newest first, with Mark read on unread rows. `basePath` is the gateway path
 * of the list ("/v1/me/notifications" or "/v1/notifications"); reads post to `${basePath}/{id}/read`.
 */
export function NotificationFeed(props: {
  basePath: string;
  auth: Extract<ApiAuth, "user" | "merchantKey">;
  rowActions?: (item: NotificationItem, actions: RowActions) => ReactNode;
}): ReactNode {
  const { client } = useServices();
  const { basePath, auth } = props;
  const [items, setItems] = useState<NotificationItem[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());

  const load = useCallback(
    async (after: string | null): Promise<void> => {
      const result = await client.request<NotificationPage>({
        base: "gw",
        path: basePath,
        auth,
        query: { limit: PAGE_SIZE, cursor: after ?? undefined },
      });
      setLoadError(errorMessageOf(result));
      if (!result.ok) return;
      setItems((prev) => (after === null || prev === null ? result.data.items : [...prev, ...result.data.items]));
      setCursor(result.data.next_cursor);
    },
    [client, basePath, auth],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  function update(next: NotificationItem): void {
    setItems((prev) =>
      prev === null ? prev : prev.map((item) => (item.notification_id === next.notification_id ? next : item)),
    );
  }

  async function markRead(item: NotificationItem): Promise<void> {
    setBusy((prev) => new Set(prev).add(item.notification_id));
    const result = await client.request<NotificationItem>({
      base: "gw",
      path: `${basePath}/${encodeURIComponent(item.notification_id)}/read`,
      method: "POST",
      auth,
    });
    setBusy((prev) => {
      const next = new Set(prev);
      next.delete(item.notification_id);
      return next;
    });
    if (result.ok) {
      update({ ...item, read: true });
      setFeedback(null);
    } else {
      setFeedback({ kind: "alert", text: errorMessageOf(result) ?? "" });
    }
  }

  return (
    <>
      {loadError === null ? null : <p role="alert">{loadError}</p>}
      {items === null ? null : items.length === 0 ? (
        <p className="muted">No notifications.</p>
      ) : (
        <ul className="list">
          {items.map((item) => {
            const amount = amountOf(item.payload);
            return (
              <li key={item.notification_id} className={item.read ? "read" : "unread"}>
                <strong>{notificationLabel(item.kind)}</strong>
                {amount === null ? null : <span> {amount}</span>}
                <span className="muted"> {new Date(item.created_at).toLocaleString()}</span>
                {item.read ? null : (
                  <button
                    type="button"
                    className="link"
                    disabled={busy.has(item.notification_id)}
                    onClick={() => void markRead(item)}
                  >
                    Mark read
                  </button>
                )}
                {props.rowActions?.(item, { update, report: setFeedback })}
              </li>
            );
          })}
        </ul>
      )}
      {cursor === null ? null : (
        <button type="button" onClick={() => void load(cursor)}>
          Load more
        </button>
      )}
      {feedback === null ? null : <p role={feedback.kind}>{feedback.text}</p>}
    </>
  );
}
