import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse } from "../../../app/__tests__/harness";
import { createUrlHarness, nth, type UrlRequest } from "../../../app/__tests__/urlHarness";
import { TicketQueuePanel } from "../TicketQueuePanel";

const TICKETS_PATH = "/api/ops/v1/tickets";
const OPEN_ID = "11111111-1111-4111-8111-111111111111";
const ASSIGNED_ID = "22222222-2222-4222-8222-222222222222";
const RESOLVED_ID = "33333333-3333-4333-8333-333333333333";

interface Ticket {
  ticket_id: string;
  source: string;
  queue: string;
  priority: string;
  status: string;
  title: string;
  reason: string;
  assignee: string | null;
  created_at: string;
  updated_at: string | null;
}

function ticket(overrides: Partial<Ticket> & Pick<Ticket, "ticket_id" | "title" | "status">): Ticket {
  return {
    source: "assistant_escalation",
    queue: "payments_ops",
    priority: "normal",
    reason: "merchant asked for help",
    assignee: null,
    created_at: "2026-11-05T09:00:00Z",
    updated_at: null,
    ...overrides,
  };
}

const OPEN = ticket({ ticket_id: OPEN_ID, title: "BIN spike escalation", status: "open", queue: "fraud", priority: "high" });
const ASSIGNED = ticket({
  ticket_id: ASSIGNED_ID,
  title: "Payout gap escalation",
  status: "assigned",
  queue: "settlement",
  priority: "low",
  assignee: "someone",
});
const RESOLVED = ticket({
  ticket_id: RESOLVED_ID,
  title: "Refund query escalation",
  status: "resolved",
  queue: "compliance",
  priority: "normal",
});

function rowFor(title: string): HTMLElement {
  const row = screen.getAllByTestId("ticket-row").find((el) => (el.textContent ?? "").includes(title));
  if (row === undefined) throw new Error(`no ticket row for ${title}`);
  return row;
}

function buttonNamesIn(row: HTMLElement): (string | null)[] {
  return within(row)
    .queryAllByRole("button")
    .map((button) => button.textContent);
}

interface Options {
  list?: (request: UrlRequest) => Response;
  transition?: (request: UrlRequest) => Response;
}

function signedIn(options: Options = {}) {
  const harness = createUrlHarness((request) => {
    if (request.method === "GET" && request.path === TICKETS_PATH) {
      return options.list?.(request) ?? jsonResponse(200, { tickets: [OPEN, ASSIGNED, RESOLVED] });
    }
    if (request.method === "POST" && request.path.endsWith("/transition")) {
      return options.transition?.(request) ?? jsonResponse(404, { detail: "ticket not found" });
    }
    return jsonResponse(404, { detail: "not found" });
  });
  harness.store.setMerchant("merchant-key", "ops-token");
  return harness;
}

describe("TicketQueuePanel", () => {
  afterEach(() => {
    cleanup();
  });

  describe("listing", () => {
    it("requests up to 50 tickets without a status filter using the merchant ops token", async () => {
      // Arrange
      const harness = signedIn();

      // Act
      harness.renderUi(<TicketQueuePanel />);
      await screen.findAllByTestId("ticket-row");

      // Assert
      const requests = harness.sent("GET", TICKETS_PATH);
      expect(requests).toHaveLength(1);
      expect(nth(requests, 0).query.get("limit")).toBe("50");
      expect(nth(requests, 0).query.has("status")).toBe(false);
      expect(nth(requests, 0).headers.get("Authorization")).toBe("Bearer ops-token");
    });

    it("shows the heading and one row per ticket with title, queue, priority and status", async () => {
      // Arrange
      const harness = signedIn();

      // Act
      harness.renderUi(<TicketQueuePanel />);
      const rows = await screen.findAllByTestId("ticket-row");

      // Assert
      expect(screen.getByRole("heading", { name: "Ticket queue" }).textContent).toBe("Ticket queue");
      expect(rows).toHaveLength(3);
      const open = nth(rows, 0).textContent ?? "";
      expect(open).toContain("BIN spike escalation");
      expect(open).toContain("fraud");
      expect(open).toContain("high");
      expect(open).toMatch(/open/i);
      const assigned = nth(rows, 1).textContent ?? "";
      expect(assigned).toContain("Payout gap escalation");
      expect(assigned).toContain("settlement");
      expect(assigned).toContain("low");
      expect(assigned).toMatch(/assigned/i);
    });

    it("offers Assign to me and Resolve on an open ticket", async () => {
      // Arrange
      const harness = signedIn();

      // Act
      harness.renderUi(<TicketQueuePanel />);
      await screen.findAllByTestId("ticket-row");

      // Assert
      expect(buttonNamesIn(rowFor("BIN spike escalation"))).toEqual(["Assign to me", "Resolve"]);
    });

    it("offers only Resolve on an assigned ticket", async () => {
      // Arrange
      const harness = signedIn();

      // Act
      harness.renderUi(<TicketQueuePanel />);
      await screen.findAllByTestId("ticket-row");

      // Assert
      expect(buttonNamesIn(rowFor("Payout gap escalation"))).toEqual(["Resolve"]);
    });

    it("offers no action on a resolved ticket", async () => {
      // Arrange
      const harness = signedIn();

      // Act
      harness.renderUi(<TicketQueuePanel />);
      await screen.findAllByTestId("ticket-row");

      // Assert
      expect(buttonNamesIn(rowFor("Refund query escalation"))).toEqual([]);
    });

    it("shows the mapped message in an alert when the list cannot be loaded", async () => {
      // Arrange
      const harness = signedIn({ list: () => jsonResponse(503, { detail: "try again later" }) });

      // Act
      harness.renderUi(<TicketQueuePanel />);

      // Assert
      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toBe("The service is busy. Try again later.");
      expect(screen.queryAllByTestId("ticket-row")).toHaveLength(0);
    });
  });

  describe("status filter", () => {
    it("reloads with the chosen status and drops the filter again for All", async () => {
      // Arrange
      const harness = signedIn({
        list: (request) =>
          jsonResponse(200, {
            tickets: request.query.get("status") === "open" ? [OPEN] : [OPEN, ASSIGNED, RESOLVED],
          }),
      });
      harness.renderUi(<TicketQueuePanel />);
      await screen.findAllByTestId("ticket-row");
      const select = screen.getByLabelText("Status");
      const valueOf = (label: RegExp): string => (within(select).getByRole("option", { name: label }) as HTMLOptionElement).value;

      // Act
      fireEvent.change(select, { target: { value: valueOf(/^open$/i) } });
      await waitFor(() => expect(screen.getAllByTestId("ticket-row")).toHaveLength(1));
      fireEvent.change(select, { target: { value: valueOf(/^all$/i) } });
      await waitFor(() => expect(screen.getAllByTestId("ticket-row")).toHaveLength(3));

      // Assert
      const requests = harness.sent("GET", TICKETS_PATH);
      expect(requests.map((request) => request.query.get("status"))).toEqual([null, "open", null]);
      expect(requests.map((request) => request.query.get("limit"))).toEqual(["50", "50", "50"]);
    });
  });

  describe("transitions", () => {
    it("assigns an open ticket to ops-console and shows the returned ticket", async () => {
      // Arrange
      const assigned = { ...OPEN, status: "assigned", assignee: "ops-console", updated_at: "2026-11-05T10:00:00Z" };
      const harness = signedIn({ transition: () => jsonResponse(200, assigned) });
      harness.renderUi(<TicketQueuePanel />);
      await screen.findAllByTestId("ticket-row");

      // Act
      fireEvent.click(within(rowFor("BIN spike escalation")).getByRole("button", { name: "Assign to me" }));
      await waitFor(() => expect(buttonNamesIn(rowFor("BIN spike escalation"))).toEqual(["Resolve"]));

      // Assert
      const requests = harness.sent("POST", `${TICKETS_PATH}/${OPEN_ID}/transition`);
      expect(requests).toHaveLength(1);
      expect(nth(requests, 0).body).toEqual({ status: "assigned", assignee: "ops-console" });
      expect(nth(requests, 0).headers.get("Authorization")).toBe("Bearer ops-token");
      expect(rowFor("BIN spike escalation").textContent).toMatch(/assigned/i);
      expect(screen.getAllByTestId("ticket-row")).toHaveLength(3);
    });

    it("resolves an assigned ticket with a body that has only the status", async () => {
      // Arrange
      const resolved = { ...ASSIGNED, status: "resolved", updated_at: "2026-11-05T10:00:00Z" };
      const harness = signedIn({ transition: () => jsonResponse(200, resolved) });
      harness.renderUi(<TicketQueuePanel />);
      await screen.findAllByTestId("ticket-row");

      // Act
      fireEvent.click(within(rowFor("Payout gap escalation")).getByRole("button", { name: "Resolve" }));
      await waitFor(() => expect(buttonNamesIn(rowFor("Payout gap escalation"))).toEqual([]));

      // Assert
      const requests = harness.sent("POST", `${TICKETS_PATH}/${ASSIGNED_ID}/transition`);
      expect(requests).toHaveLength(1);
      expect(nth(requests, 0).body).toEqual({ status: "resolved" });
      expect(rowFor("Payout gap escalation").textContent).toMatch(/resolved/i);
    });

    it("leaves the other rows untouched after a transition", async () => {
      // Arrange
      const assigned = { ...OPEN, status: "assigned", assignee: "ops-console" };
      const harness = signedIn({ transition: () => jsonResponse(200, assigned) });
      harness.renderUi(<TicketQueuePanel />);
      await screen.findAllByTestId("ticket-row");

      // Act
      fireEvent.click(within(rowFor("BIN spike escalation")).getByRole("button", { name: "Assign to me" }));
      await waitFor(() => expect(buttonNamesIn(rowFor("BIN spike escalation"))).toEqual(["Resolve"]));

      // Assert
      expect(buttonNamesIn(rowFor("Payout gap escalation"))).toEqual(["Resolve"]);
      expect(buttonNamesIn(rowFor("Refund query escalation"))).toEqual([]);
    });

    it("explains that only the merchant's own escalations can be changed on a 403", async () => {
      // Arrange
      const harness = signedIn({ transition: () => jsonResponse(403, { detail: "ticket_not_merchant_owned" }) });
      harness.renderUi(<TicketQueuePanel />);
      await screen.findAllByTestId("ticket-row");

      // Act
      fireEvent.click(within(rowFor("BIN spike escalation")).getByRole("button", { name: "Assign to me" }));

      // Assert
      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toBe("Only this merchant's own escalations can be changed here.");
      expect(buttonNamesIn(rowFor("BIN spike escalation"))).toEqual(["Assign to me", "Resolve"]);
    });

    it("asks to refresh the queue on a 409 conflict", async () => {
      // Arrange
      const harness = signedIn({ transition: () => jsonResponse(409, { detail: "invalid ticket transition" }) });
      harness.renderUi(<TicketQueuePanel />);
      await screen.findAllByTestId("ticket-row");

      // Act
      fireEvent.click(within(rowFor("Payout gap escalation")).getByRole("button", { name: "Resolve" }));

      // Assert
      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toBe("That ticket already moved on. Refresh the queue.");
      expect(buttonNamesIn(rowFor("Payout gap escalation"))).toEqual(["Resolve"]);
    });

    it("clears the earlier error after a later transition succeeds", async () => {
      // Arrange
      let call = 0;
      const assigned = { ...OPEN, status: "assigned", assignee: "ops-console" };
      const harness = signedIn({
        transition: () => {
          call += 1;
          return call === 1 ? jsonResponse(409, { detail: "invalid ticket transition" }) : jsonResponse(200, assigned);
        },
      });
      harness.renderUi(<TicketQueuePanel />);
      await screen.findAllByTestId("ticket-row");
      fireEvent.click(within(rowFor("BIN spike escalation")).getByRole("button", { name: "Assign to me" }));
      await screen.findByRole("alert");

      // Act
      fireEvent.click(within(rowFor("BIN spike escalation")).getByRole("button", { name: "Assign to me" }));
      await waitFor(() => expect(buttonNamesIn(rowFor("BIN spike escalation"))).toEqual(["Resolve"]));

      // Assert
      expect(screen.queryAllByRole("alert")).toHaveLength(0);
    });
  });
});
