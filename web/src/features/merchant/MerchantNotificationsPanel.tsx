import type { ReactNode } from "react";
import { NotificationFeed } from "../../components/NotificationFeed";

/** B11.1: the merchant's in-app notifications (refund requests, disputes, payment outcomes). */
export function MerchantNotificationsPanel(): ReactNode {
  return (
    <section aria-labelledby="merchant-notifications-title">
      <h2 id="merchant-notifications-title">Notifications</h2>
      <NotificationFeed basePath="/v1/notifications" auth="merchantKey" />
    </section>
  );
}
