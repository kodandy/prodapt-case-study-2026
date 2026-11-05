import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse, type Responder } from "../../../app/__tests__/harness";
import { createQueryHarness } from "../../../app/__tests__/queryHarness";
import { PaymentsHistoryPanel } from "../PaymentsHistoryPanel";

const LIST_PATH = "/api/gw/v1/me/payments";
const P1 = "11111111-1111-4111-8111-111111111111";
const P2 = "22222222-2222-4222-8222-222222222222";
const P3 = "33333333-3333-4333-8333-333333333333";
const P4 = "44444444-4444-4444-8444-444444444444";

interface ConsumerPaymentFixture {
  payment_id: string;
  merchant_id: string;
  state: string;
  amount_minor: number;
  currency: string;
  card: { bin: string; last4: string } | null;
  decline_code: string | null;
  refunded_minor: number;
  disputed_minor: number;
  chargeback_minor: number;
  chargeback_flag: boolean;
  created_at: string;
}

interface PageFixture {
  items: ConsumerPaymentFixture[];
  next_cursor: string | null;
}

function payment(id: string, overrides: Partial<ConsumerPaymentFixture> = {}): ConsumerPaymentFixture {
  return {
    payment_id: id,
    merchant_id: "9c1d7e22-0b5a-4c11-8a3e-5d6f7a8b9c0d",
    state: "succeeded",
    amount_minor: 2500,
    currency: "USD",
    card: { bin: "424242", last4: "4242" },
    decline_code: null,
    refunded_minor: 0,
    disputed_minor: 0,
    chargeback_minor: 0,
    chargeback_flag: false,
    created_at: "2026-10-01T12:00:00Z",
    ...overrides,
  };
}

const SUCCEEDED_P1 = payment(P1);
const DECLINED_P2 = payment(P2, { state: "declined", decline_code: "card_declined", card: { bin: "400000", last4: "0002" } });
const SUCCEEDED_P3 = payment(P3, { amount_minor: 900 });
const FLAGGED_P4 = payment(P4, { chargeback_flag: true, chargeback_minor: 2500 });

function page(items: ConsumerPaymentFixture[], nextCursor: string | null = null): PageFixture {
  return { items, next_cursor: nextCursor };
}

/** The n-th GET of the list answers pages[n]; later GETs repeat the last page. Other requests use `other`. */
function serving(pages: PageFixture[], other: Responder = () => jsonResponse(404, { detail: "not found" })): Responder {
  let served = 0;
  return (request) => {
    if (request.method === "GET" && request.path === LIST_PATH) {
      const next = pages[Math.min(served, pages.length - 1)];
      served += 1;
      return jsonResponse(200, next);
    }
    return other(request);
  };
}

function setup(responder: Responder) {
  const harness = createQueryHarness(responder);
  harness.store.setUserSession("user-token");
  harness.renderUi(<PaymentsHistoryPanel />);
  return harness;
}

function refundButtons(): HTMLElement[] {
  return screen.queryAllByRole("button", { name: /Request refund/ });
}

function disputeButtons(): HTMLElement[] {
  return screen.queryAllByRole("button", { name: /Open dispute/ });
}

async function loaded(count: number): Promise<void> {
  await waitFor(() => expect(refundButtons()).toHaveLength(count));
}

function feedbackText(): string {
  return [...screen.queryAllByRole("alert"), ...screen.queryAllByRole("status")]
    .map((element) => element.textContent ?? "")
    .join(" ");
}

function applyFilters(fields: { status?: string; from?: string; to?: string }): void {
  if (fields.status !== undefined) fireEvent.change(screen.getByLabelText("Status"), { target: { value: fields.status } });
  if (fields.from !== undefined) fireEvent.change(screen.getByLabelText("From"), { target: { value: fields.from } });
  if (fields.to !== undefined) fireEvent.change(screen.getByLabelText("To"), { target: { value: fields.to } });
  fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
}

function chargebackMarks(): number {
  return screen.queryAllByText("Chargeback").length;
}

function click(element: HTMLElement | undefined): void {
  if (element === undefined) throw new Error("expected element to exist");
  fireEvent.click(element);
}

describe("PaymentsHistoryPanel", () => {
  afterEach(() => {
    cleanup();
  });

  describe("structure and initial load", () => {
    it("labels the section by its Payments heading", async () => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1])]));

      // Act
      const region = screen.getByRole("region", { name: "Payments" });
      await loaded(1);

      // Assert
      expect(region.querySelector("h2")?.textContent).toBe("Payments");
    });

    it("loads the first page with limit 20 and the user session, without filters", async () => {
      // Arrange
      const harness = setup(serving([page([SUCCEEDED_P1])]));

      // Act
      await loaded(1);

      // Assert
      const gets = harness.sent("GET", LIST_PATH);
      expect(gets).toHaveLength(1);
      expect(gets[0]?.headers.get("Authorization")).toBe("Bearer user-token");
      const query = harness.queriesOf("GET", LIST_PATH)[0];
      expect(query?.get("limit")).toBe("20");
      expect(query?.has("cursor")).toBe(false);
      expect(query?.has("state")).toBe(false);
      expect(query?.has("created_from")).toBe(false);
      expect(query?.has("created_to")).toBe(false);
    });

    it("offers the status options All, succeeded, declined and failed", () => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1])]));

      // Act
      const options = Array.from(screen.getByLabelText("Status").querySelectorAll("option"));

      // Assert
      expect(options.map((o) => o.value)).toEqual(["", "succeeded", "declined", "failed"]);
      expect(options[0]?.textContent).toBe("All");
    });

    it("uses date inputs for From and To", () => {
      // Arrange
      setup(serving([page([])]));

      // Act
      const from = screen.getByLabelText("From");
      const to = screen.getByLabelText("To");

      // Assert
      expect(from.getAttribute("type")).toBe("date");
      expect(to.getAttribute("type")).toBe("date");
    });

    it("shows a role alert and no row actions when the list cannot be loaded", async () => {
      // Arrange
      setup(() => jsonResponse(500, { detail: "boom" }));

      // Act
      const alert = await screen.findByRole("alert");

      // Assert
      expect(alert.textContent).not.toBe("");
      expect(refundButtons()).toHaveLength(0);
      expect(disputeButtons()).toHaveLength(0);
    });
  });

  describe("row actions", () => {
    it("offers Request refund and Open dispute only on succeeded payments", async () => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1, DECLINED_P2, SUCCEEDED_P3])]));

      // Act
      await loaded(2);

      // Assert
      expect(refundButtons()).toHaveLength(2);
      expect(disputeButtons()).toHaveLength(2);
    });

    it("shows no Chargeback mark when no payment has the flag", async () => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1, SUCCEEDED_P3])]));

      // Act
      await loaded(2);

      // Assert
      expect(chargebackMarks()).toBe(0);
    });

    it("shows a Chargeback mark for a payment whose chargeback_flag is true", async () => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1, FLAGGED_P4])]));

      // Act
      await loaded(2);

      // Assert
      expect(chargebackMarks()).toBeGreaterThan(0);
    });
  });

  describe("refund request", () => {
    it("posts an empty body to the refund-requests path of the chosen payment", async () => {
      // Arrange
      const harness = setup(
        serving([page([SUCCEEDED_P1, DECLINED_P2, SUCCEEDED_P3])], () =>
          jsonResponse(201, { payment_id: P3, requested_minor: 900, state: "requested" }),
        ),
      );
      await loaded(2);

      // Act
      click(refundButtons()[1]);

      // Assert
      await screen.findByText("Refund requested.");
      const posts = harness.sent("POST", `${LIST_PATH}/${P3}/refund-requests`);
      expect(posts).toHaveLength(1);
      expect(posts[0]?.body).toEqual({});
      expect(posts[0]?.headers.get("Authorization")).toBe("Bearer user-token");
      expect(harness.sent("POST", `${LIST_PATH}/${P1}/refund-requests`)).toHaveLength(0);
    });

    it("shows the Refund requested. message as a status", async () => {
      // Arrange
      setup(
        serving([page([SUCCEEDED_P1])], () =>
          jsonResponse(201, { payment_id: P1, requested_minor: 2500, state: "requested" }),
        ),
      );
      await loaded(1);

      // Act
      click(refundButtons()[0]);

      // Assert
      const status = await screen.findByText("Refund requested.");
      expect(status.getAttribute("role")).toBe("status");
    });

    it.each([
      { name: "refund_already_requested", status: 409, body: { error: "refund_already_requested" } },
      { name: "payment_not_refundable", status: 409, body: { error: "payment_not_refundable" } },
      { name: "payment_not_found", status: 404, body: { error: "payment_not_found" } },
    ])("shows an error and no success message for $name", async ({ status, body }) => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1])], () => jsonResponse(status, body)));
      await loaded(1);

      // Act
      click(refundButtons()[0]);

      // Assert
      await waitFor(() => expect(feedbackText()).not.toBe(""));
      expect(feedbackText()).not.toContain("Refund requested.");
    });
  });

  describe("dispute", () => {
    const DISPUTE_VIEW = {
      dispute_id: "d-1",
      payment_id: P3,
      state: "opened",
      amount_minor: 900,
      currency: "USD",
      reason: "product_not_received",
      chargeback_fee_minor: 0,
      created_at: "2026-10-02T12:00:00Z",
      updated_at: "2026-10-02T12:00:00Z",
    };

    it("hides the Reason select until Open dispute is pressed", async () => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1])]));
      await loaded(1);
      const reasonBefore = screen.queryByLabelText("Reason");

      // Act
      click(disputeButtons()[0]);

      // Assert
      expect(reasonBefore).toBeNull();
      expect(screen.getByLabelText("Reason").tagName).toBe("SELECT");
      expect(screen.getByRole("button", { name: "Submit dispute" }).textContent).toBe("Submit dispute");
    });

    it("offers the four dispute reasons", async () => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1])]));
      await loaded(1);

      // Act
      click(disputeButtons()[0]);

      // Assert
      const options = Array.from(screen.getByLabelText("Reason").querySelectorAll("option"));
      expect(options.map((o) => o.value)).toEqual(["fraudulent", "product_not_received", "duplicate", "other"]);
    });

    it("posts the chosen reason for the chosen payment and shows Dispute opened.", async () => {
      // Arrange
      const harness = setup(serving([page([SUCCEEDED_P1, SUCCEEDED_P3])], () => jsonResponse(201, DISPUTE_VIEW)));
      await loaded(2);
      click(disputeButtons()[1]);
      fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "product_not_received" } });

      // Act
      fireEvent.click(screen.getByRole("button", { name: "Submit dispute" }));

      // Assert
      await screen.findByText("Dispute opened.");
      const posts = harness.sent("POST", `${LIST_PATH}/${P3}/disputes`);
      expect(posts).toHaveLength(1);
      expect(posts[0]?.body).toEqual({ reason: "product_not_received" });
      expect(posts[0]?.headers.get("Authorization")).toBe("Bearer user-token");
      expect(harness.sent("POST", `${LIST_PATH}/${P1}/disputes`)).toHaveLength(0);
    });

    it.each([
      { name: "payment not disputable", body: { detail: "payment not disputable" } },
      { name: "dispute already open", body: { detail: "dispute already open" } },
    ])("shows an error in an alert or status for a 409 $name", async ({ body }) => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1])], () => jsonResponse(409, body)));
      await loaded(1);
      click(disputeButtons()[0]);
      fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "fraudulent" } });

      // Act
      fireEvent.click(screen.getByRole("button", { name: "Submit dispute" }));

      // Assert
      await waitFor(() => expect(feedbackText()).not.toBe(""));
      expect(feedbackText()).not.toContain("Dispute opened.");
    });
  });

  describe("filters", () => {
    it("sends state and a half-open UTC date range when Apply filters is pressed", async () => {
      // Arrange
      const harness = setup(serving([page([SUCCEEDED_P1])]));
      await loaded(1);

      // Act
      applyFilters({ status: "succeeded", from: "2026-10-01", to: "2026-10-30" });

      // Assert
      await waitFor(() => expect(harness.queriesOf("GET", LIST_PATH)).toHaveLength(2));
      const query = harness.queriesOf("GET", LIST_PATH)[1];
      expect(query?.get("state")).toBe("succeeded");
      expect(query?.get("created_from")).toBe("2026-10-01T00:00:00Z");
      expect(query?.get("created_to")).toBe("2026-10-31T00:00:00Z");
      expect(query?.get("limit")).toBe("20");
      expect(query?.has("cursor")).toBe(false);
    });

    it.each([
      { to: "2026-10-31", expected: "2026-11-01T00:00:00Z" },
      { to: "2026-12-31", expected: "2027-01-01T00:00:00Z" },
      { to: "2028-02-28", expected: "2028-02-29T00:00:00Z" },
    ])("ends the range at the start of the day after $to", async ({ to, expected }) => {
      // Arrange
      const harness = setup(serving([page([SUCCEEDED_P1])]));
      await loaded(1);

      // Act
      applyFilters({ to });

      // Assert
      await waitFor(() => expect(harness.queriesOf("GET", LIST_PATH)).toHaveLength(2));
      expect(harness.queriesOf("GET", LIST_PATH)[1]?.get("created_to")).toBe(expected);
    });

    it("omits the parameters of filters that are left empty", async () => {
      // Arrange
      const harness = setup(serving([page([SUCCEEDED_P1])]));
      await loaded(1);

      // Act
      applyFilters({ from: "2026-10-01" });

      // Assert
      await waitFor(() => expect(harness.queriesOf("GET", LIST_PATH)).toHaveLength(2));
      const query = harness.queriesOf("GET", LIST_PATH)[1];
      expect(query?.get("created_from")).toBe("2026-10-01T00:00:00Z");
      expect(query?.has("created_to")).toBe(false);
      expect(query?.has("state")).toBe(false);
    });

    it("replaces the list with the filtered result", async () => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1, SUCCEEDED_P3]), page([DECLINED_P2])]));
      await loaded(2);

      // Act
      applyFilters({ status: "declined" });

      // Assert
      await loaded(0);
      expect(disputeButtons()).toHaveLength(0);
    });
  });

  describe("pagination", () => {
    it("shows no Load more when there is no next_cursor", async () => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1])]));

      // Act
      await loaded(1);

      // Assert
      expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    });

    it("sends the cursor and appends the next page when Load more is pressed", async () => {
      // Arrange
      const harness = setup(serving([page([SUCCEEDED_P1], "cursor-1"), page([SUCCEEDED_P3])]));
      await loaded(1);

      // Act
      fireEvent.click(screen.getByRole("button", { name: "Load more" }));

      // Assert
      await loaded(2);
      const queries = harness.queriesOf("GET", LIST_PATH);
      expect(queries).toHaveLength(2);
      expect(queries[1]?.get("cursor")).toBe("cursor-1");
      expect(queries[1]?.get("limit")).toBe("20");
    });

    it("removes Load more once the last page is loaded", async () => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1], "cursor-1"), page([SUCCEEDED_P3])]));
      await loaded(1);

      // Act
      fireEvent.click(screen.getByRole("button", { name: "Load more" }));

      // Assert
      await loaded(2);
      expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    });

    it("keeps the applied filters when loading more", async () => {
      // Arrange
      const harness = setup(serving([page([SUCCEEDED_P1]), page([SUCCEEDED_P1], "cursor-2"), page([SUCCEEDED_P3])]));
      await loaded(1);
      applyFilters({ status: "succeeded", from: "2026-10-01" });
      const loadMore = await screen.findByRole("button", { name: "Load more" });

      // Act
      fireEvent.click(loadMore);

      // Assert
      await waitFor(() => expect(harness.queriesOf("GET", LIST_PATH)).toHaveLength(3));
      const query = harness.queriesOf("GET", LIST_PATH)[2];
      expect(query?.get("cursor")).toBe("cursor-2");
      expect(query?.get("state")).toBe("succeeded");
      expect(query?.get("created_from")).toBe("2026-10-01T00:00:00Z");
    });

    it("shows a Chargeback mark for a flagged payment loaded in a later page", async () => {
      // Arrange
      setup(serving([page([SUCCEEDED_P1], "cursor-1"), page([FLAGGED_P4])]));
      await loaded(1);
      const marksBefore = chargebackMarks();

      // Act
      fireEvent.click(screen.getByRole("button", { name: "Load more" }));

      // Assert
      await loaded(2);
      expect(chargebackMarks()).toBe(marksBefore + 1);
    });
  });
});
