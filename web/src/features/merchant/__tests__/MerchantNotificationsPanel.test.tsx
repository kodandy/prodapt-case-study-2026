import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse, type Responder } from "../../../app/__tests__/harness";
import { createQueryHarness } from "../../../app/__tests__/queryHarness";
import { MerchantNotificationsPanel } from "../MerchantNotificationsPanel";

const LIST_PATH = "/api/gw/v1/notifications";
const PAYMENT_ID = "6d0f1c52-8b0e-4a31-a1d7-2f6f3c9b7e10";

interface NotificationFixture {
  notification_id: string;
  kind: string;
  payment_id: string | null;
  payload: Record<string, string | number>;
  created_at: string;
  read: boolean;
  response: null;
}

function notification(id: string, kind: string, read = false): NotificationFixture {
  return {
    notification_id: id,
    kind,
    payment_id: PAYMENT_ID,
    payload: {},
    created_at: "2026-10-01T12:00:00Z",
    read,
    response: null,
  };
}

const REFUND_REQUESTED = notification("m-refund", "refund_requested");
const DISPUTE_OPENED = notification("m-dispute", "dispute_opened");
const CONFIRMATION_READ = notification("m-conf", "payment_confirmation", true);

function serving(items: NotificationFixture[], other: Responder = () => jsonResponse(404, { detail: "not found" })): Responder {
  return (request) =>
    request.method === "GET" && request.path === LIST_PATH
      ? jsonResponse(200, { items, next_cursor: null })
      : other(request);
}

function setup(responder: Responder) {
  const harness = createQueryHarness(responder);
  harness.store.setUserSession("user-token");
  harness.store.setMerchant("merchant-api-key", "ops-token");
  harness.renderUi(<MerchantNotificationsPanel />);
  return harness;
}

function markReadButtons(): HTMLElement[] {
  return screen.queryAllByRole("button", { name: /Mark read/ });
}

function feedbackText(): string {
  return [...screen.queryAllByRole("alert"), ...screen.queryAllByRole("status")]
    .map((element) => element.textContent ?? "")
    .join(" ");
}

function click(element: HTMLElement | undefined): void {
  if (element === undefined) throw new Error("expected element to exist");
  fireEvent.click(element);
}

describe("MerchantNotificationsPanel", () => {
  afterEach(() => {
    cleanup();
  });

  describe("structure and initial load", () => {
    it("labels the section by its Notifications heading", async () => {
      // Arrange
      setup(serving([REFUND_REQUESTED]));

      // Act
      const region = screen.getByRole("region", { name: "Notifications" });
      await waitFor(() => expect(markReadButtons()).toHaveLength(1));

      // Assert
      expect(region.querySelector("h2")?.textContent).toBe("Notifications");
    });

    it("loads the first page with limit 20 using the merchant API key, not the user session", async () => {
      // Arrange
      const harness = setup(serving([REFUND_REQUESTED]));

      // Act
      await waitFor(() => expect(markReadButtons()).toHaveLength(1));

      // Assert
      const gets = harness.sent("GET", LIST_PATH);
      expect(gets).toHaveLength(1);
      expect(gets[0]?.headers.get("Authorization")).toBe("Bearer merchant-api-key");
      expect(harness.queriesOf("GET", LIST_PATH)[0]?.get("limit")).toBe("20");
    });

    it("shows refund_requested as Refund requested and dispute_opened as Dispute opened", async () => {
      // Arrange
      setup(serving([REFUND_REQUESTED, DISPUTE_OPENED]));

      // Act
      await waitFor(() => expect(markReadButtons()).toHaveLength(2));

      // Assert
      expect(screen.queryAllByText("Refund requested")).toHaveLength(1);
      expect(screen.queryAllByText("Dispute opened")).toHaveLength(1);
      expect(screen.queryAllByText("refund_requested")).toHaveLength(0);
      expect(screen.queryAllByText("dispute_opened")).toHaveLength(0);
    });

    it("shows a role alert when the list cannot be loaded", async () => {
      // Arrange
      setup(() => jsonResponse(500, { detail: "boom" }));

      // Act
      const alert = await screen.findByRole("alert");

      // Assert
      expect(alert.textContent).not.toBe("");
      expect(markReadButtons()).toHaveLength(0);
    });
  });

  describe("mark read", () => {
    it("offers Mark read only on unread notifications", async () => {
      // Arrange
      setup(serving([REFUND_REQUESTED, CONFIRMATION_READ]));

      // Act
      await waitFor(() => expect(markReadButtons()).toHaveLength(1));

      // Assert
      expect(markReadButtons()).toHaveLength(1);
    });

    it("posts to the read path of that notification with the merchant key and removes its button", async () => {
      // Arrange
      const harness = setup(
        serving([REFUND_REQUESTED, DISPUTE_OPENED], () => jsonResponse(200, { ...DISPUTE_OPENED, read: true })),
      );
      await waitFor(() => expect(markReadButtons()).toHaveLength(2));

      // Act
      click(markReadButtons()[1]);

      // Assert
      await waitFor(() => expect(markReadButtons()).toHaveLength(1));
      const posts = harness.sent("POST", `${LIST_PATH}/m-dispute/read`);
      expect(posts).toHaveLength(1);
      expect(posts[0]?.headers.get("Authorization")).toBe("Bearer merchant-api-key");
      expect(harness.sent("POST", `${LIST_PATH}/m-refund/read`)).toHaveLength(0);
    });

    it("shows an error and keeps the button when the notification is not found", async () => {
      // Arrange
      setup(serving([REFUND_REQUESTED], () => jsonResponse(404, { detail: "notification not found" })));
      await waitFor(() => expect(markReadButtons()).toHaveLength(1));

      // Act
      click(markReadButtons()[0]);

      // Assert
      await waitFor(() => expect(feedbackText()).not.toBe(""));
      expect(markReadButtons()).toHaveLength(1);
    });
  });
});
