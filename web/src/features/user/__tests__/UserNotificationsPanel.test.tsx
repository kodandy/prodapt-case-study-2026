import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse, type Responder } from "../../../app/__tests__/harness";
import { createQueryHarness } from "../../../app/__tests__/queryHarness";
import { UserNotificationsPanel } from "../UserNotificationsPanel";

const LIST_PATH = "/api/gw/v1/me/notifications";
const PAYMENT_ID = "6d0f1c52-8b0e-4a31-a1d7-2f6f3c9b7e10";

interface NotificationFixture {
  notification_id: string;
  kind: string;
  payment_id: string | null;
  payload: Record<string, string | number>;
  created_at: string;
  read: boolean;
  response: "confirmed" | "not_me" | null;
}

function notification(id: string, kind: string, overrides: Partial<NotificationFixture> = {}): NotificationFixture {
  return {
    notification_id: id,
    kind,
    payment_id: PAYMENT_ID,
    payload: {},
    created_at: "2026-10-01T12:00:00Z",
    read: false,
    response: null,
    ...overrides,
  };
}

const RECEIPT = notification("n-receipt", "payment_receipt");
const WAS_THIS_YOU = notification("n-wty", "was_this_you");
const DISPUTE_UPDATE = notification("n-upd", "dispute_update", { read: true });
const ANSWERED = notification("n-done", "was_this_you", { read: true, response: "confirmed" });

function serving(items: NotificationFixture[], other: Responder = () => jsonResponse(404, { detail: "not found" })): Responder {
  return (request) =>
    request.method === "GET" && request.path === LIST_PATH
      ? jsonResponse(200, { items, next_cursor: null })
      : other(request);
}

function setup(responder: Responder) {
  const harness = createQueryHarness(responder);
  harness.store.setUserSession("user-token");
  harness.renderUi(<UserNotificationsPanel />);
  return harness;
}

function markReadButtons(): HTMLElement[] {
  return screen.queryAllByRole("button", { name: /Mark read/ });
}

function yesButtons(): HTMLElement[] {
  return screen.queryAllByRole("button", { name: "Yes, it was me" });
}

function noButtons(): HTMLElement[] {
  return screen.queryAllByRole("button", { name: "No, it wasn't me" });
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

describe("UserNotificationsPanel", () => {
  afterEach(() => {
    cleanup();
  });

  describe("structure and initial load", () => {
    it("labels the section by its Notifications heading", async () => {
      // Arrange
      setup(serving([RECEIPT]));

      // Act
      const region = screen.getByRole("region", { name: "Notifications" });
      await waitFor(() => expect(markReadButtons()).toHaveLength(1));

      // Assert
      expect(region.querySelector("h2")?.textContent).toBe("Notifications");
    });

    it("loads the first page with limit 20 and the user session", async () => {
      // Arrange
      const harness = setup(serving([RECEIPT]));

      // Act
      await waitFor(() => expect(markReadButtons()).toHaveLength(1));

      // Assert
      const gets = harness.sent("GET", LIST_PATH);
      expect(gets).toHaveLength(1);
      expect(gets[0]?.headers.get("Authorization")).toBe("Bearer user-token");
      expect(harness.queriesOf("GET", LIST_PATH)[0]?.get("limit")).toBe("20");
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

    it("shows a human readable label for was_this_you instead of the raw kind", async () => {
      // Arrange
      setup(serving([WAS_THIS_YOU, RECEIPT]));

      // Act
      await waitFor(() => expect(yesButtons()).toHaveLength(1));

      // Assert
      expect(screen.queryAllByText("Was this you?").length).toBeGreaterThan(0);
      expect(screen.queryAllByText("was_this_you")).toHaveLength(0);
      expect(screen.queryAllByText("payment_receipt")).toHaveLength(0);
    });
  });

  describe("mark read", () => {
    it("offers Mark read only on unread notifications", async () => {
      // Arrange
      setup(serving([RECEIPT, DISPUTE_UPDATE, ANSWERED]));

      // Act
      await waitFor(() => expect(markReadButtons()).toHaveLength(1));

      // Assert
      expect(markReadButtons()).toHaveLength(1);
    });

    it("posts to the read path of that notification and removes its button", async () => {
      // Arrange
      const harness = setup(
        serving([RECEIPT, DISPUTE_UPDATE], () => jsonResponse(200, { ...RECEIPT, read: true })),
      );
      await waitFor(() => expect(markReadButtons()).toHaveLength(1));

      // Act
      click(markReadButtons()[0]);

      // Assert
      await waitFor(() => expect(markReadButtons()).toHaveLength(0));
      const posts = harness.sent("POST", `${LIST_PATH}/n-receipt/read`);
      expect(posts).toHaveLength(1);
      expect(posts[0]?.headers.get("Authorization")).toBe("Bearer user-token");
    });

    it("keeps the other unread notifications markable", async () => {
      // Arrange
      const second = notification("n-second", "payment_receipt");
      setup(serving([RECEIPT, second], () => jsonResponse(200, { ...RECEIPT, read: true })));
      await waitFor(() => expect(markReadButtons()).toHaveLength(2));

      // Act
      click(markReadButtons()[0]);

      // Assert
      await waitFor(() => expect(markReadButtons()).toHaveLength(1));
    });

    it("shows an error and keeps the button when the notification is not found", async () => {
      // Arrange
      setup(serving([RECEIPT], () => jsonResponse(404, { detail: "notification not found" })));
      await waitFor(() => expect(markReadButtons()).toHaveLength(1));

      // Act
      click(markReadButtons()[0]);

      // Assert
      await waitFor(() => expect(feedbackText()).not.toBe(""));
      expect(markReadButtons()).toHaveLength(1);
    });
  });

  describe("was this you", () => {
    it("offers Yes and No only on was_this_you notices without a response", async () => {
      // Arrange
      setup(serving([WAS_THIS_YOU, ANSWERED, RECEIPT]));

      // Act
      await waitFor(() => expect(yesButtons()).toHaveLength(1));

      // Assert
      expect(yesButtons()).toHaveLength(1);
      expect(noButtons()).toHaveLength(1);
    });

    it("posts confirmed and removes both buttons when the answer is Yes", async () => {
      // Arrange
      const harness = setup(
        serving([WAS_THIS_YOU], () =>
          jsonResponse(200, { notification_id: "n-wty", response: "confirmed", dispute_id: null }),
        ),
      );
      await waitFor(() => expect(yesButtons()).toHaveLength(1));

      // Act
      click(yesButtons()[0]);

      // Assert
      await waitFor(() => expect(yesButtons()).toHaveLength(0));
      expect(noButtons()).toHaveLength(0);
      const posts = harness.sent("POST", `${LIST_PATH}/n-wty/response`);
      expect(posts).toHaveLength(1);
      expect(posts[0]?.body).toEqual({ answer: "confirmed" });
      expect(posts[0]?.headers.get("Authorization")).toBe("Bearer user-token");
    });

    it("posts not_me, says a dispute was opened and removes both buttons when the answer is No", async () => {
      // Arrange
      const harness = setup(
        serving([WAS_THIS_YOU], () =>
          jsonResponse(200, { notification_id: "n-wty", response: "not_me", dispute_id: "d-1" }),
        ),
      );
      await waitFor(() => expect(noButtons()).toHaveLength(1));

      // Act
      click(noButtons()[0]);

      // Assert
      await waitFor(() => expect(feedbackText()).toMatch(/dispute/i));
      expect(yesButtons()).toHaveLength(0);
      expect(noButtons()).toHaveLength(0);
      expect(harness.sent("POST", `${LIST_PATH}/n-wty/response`)[0]?.body).toEqual({ answer: "not_me" });
    });

    it("removes the buttons without mentioning a dispute when not_me opened none", async () => {
      // Arrange
      setup(
        serving([WAS_THIS_YOU], () =>
          jsonResponse(200, { notification_id: "n-wty", response: "not_me", dispute_id: null }),
        ),
      );
      await waitFor(() => expect(noButtons()).toHaveLength(1));

      // Act
      click(noButtons()[0]);

      // Assert
      await waitFor(() => expect(noButtons()).toHaveLength(0));
      expect(yesButtons()).toHaveLength(0);
      expect(feedbackText()).not.toMatch(/dispute/i);
    });

    it("shows an error when the notice was already answered", async () => {
      // Arrange
      setup(serving([WAS_THIS_YOU], () => jsonResponse(409, { detail: "already answered" })));
      await waitFor(() => expect(yesButtons()).toHaveLength(1));

      // Act
      click(yesButtons()[0]);

      // Assert
      await waitFor(() => expect(feedbackText()).not.toBe(""));
      expect(feedbackText()).not.toMatch(/dispute/i);
    });
  });
});
