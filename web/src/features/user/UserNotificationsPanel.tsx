import { useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { NotificationFeed, type NotificationItem, type RowActions } from "../../components/NotificationFeed";

const BASE_PATH = "/v1/me/notifications";

interface AnswerResponse {
  notification_id: string;
  response: "confirmed" | "not_me";
  dispute_id: string | null;
}

/** B11.1 / B11.2: the user's notifications; a "was this you?" notice takes one answer, and "No" disputes it. */
export function UserNotificationsPanel(): ReactNode {
  return (
    <section aria-labelledby="user-notifications-title">
      <h2 id="user-notifications-title">Notifications</h2>
      <NotificationFeed
        basePath={BASE_PATH}
        auth="user"
        rowActions={(item, actions) =>
          item.kind === "was_this_you" && item.response === null ? (
            <WasThisYouActions item={item} actions={actions} />
          ) : null
        }
      />
    </section>
  );
}

function WasThisYouActions(props: { item: NotificationItem; actions: RowActions }): ReactNode {
  const { client } = useServices();
  const { item, actions } = props;
  const [sending, setSending] = useState(false);

  async function answer(choice: "confirmed" | "not_me"): Promise<void> {
    setSending(true);
    const result = await client.request<AnswerResponse>({
      base: "gw",
      path: `${BASE_PATH}/${encodeURIComponent(item.notification_id)}/response`,
      method: "POST",
      auth: "user",
      body: { answer: choice },
    });
    setSending(false);
    if (!result.ok) {
      actions.report({ kind: "alert", text: errorMessageOf(result) ?? "" });
      return;
    }
    actions.update({ ...item, read: true, response: result.data.response });
    if (result.data.response === "confirmed") {
      actions.report({ kind: "status", text: "Thanks for confirming." });
    } else if (result.data.dispute_id !== null) {
      actions.report({ kind: "status", text: "We opened a dispute for this payment." });
    } else {
      actions.report({ kind: "status", text: "Thanks, we have recorded your answer." });
    }
  }

  return (
    <span className="actions">
      <button type="button" disabled={sending} onClick={() => void answer("confirmed")}>
        Yes, it was me
      </button>
      <button type="button" disabled={sending} onClick={() => void answer("not_me")}>
        No, it wasn't me
      </button>
    </span>
  );
}
