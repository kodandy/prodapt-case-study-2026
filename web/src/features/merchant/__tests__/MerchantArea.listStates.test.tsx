import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { createHarness, jsonResponse, pendingResponse, type Responder } from "../../../app/__tests__/harness";
import { MerchantArea } from "../MerchantArea";
import type { PaymentSummary } from "../types";

const PAYMENTS_PATH = "/api/gw/v1/payments";

function paymentOf(id: string, state: string): PaymentSummary {
  return {
    payment_id: id,
    state,
    amount_minor: 1250,
    currency: "USD",
    method: "card",
    card: { bin: "411111", last4: "1111" },
    decline_code: null,
    refunded_minor: 0,
    disputed_minor: 0,
    chargeback_minor: 0,
    created_at: "2026-10-01T12:00:00Z",
  };
}

function setup(payments: Responder) {
  const harness = createHarness((request) => {
    if (request.method === "GET" && request.path === PAYMENTS_PATH) return payments(request);
    return jsonResponse(404, { detail: "not found" });
  });
  harness.store.setMerchant("merchant-api-key", "ops-token");
  harness.renderUi(<MerchantArea />);
  return harness;
}

function paymentRowIds(): (string | null)[] {
  return screen.queryAllByTestId("payment-row").map((row) => row.getAttribute("data-payment-id"));
}

function paymentsSection(): HTMLElement {
  return screen.getByRole("region", { name: "Payments" });
}

describe("MerchantArea payments list", () => {
  afterEach(() => {
    cleanup();
  });

  describe("State filter", () => {
    it("offers exactly the states the payments API accepts", async () => {
      // Arrange
      setup(() => jsonResponse(200, { items: [], next_cursor: null }));
      await screen.findByText("No payments.");

      // Act
      const options = within(screen.getByLabelText("State")).getAllByRole("option") as HTMLOptionElement[];

      // Assert
      expect(options.map((o) => o.value)).toEqual(["", "created", "succeeded", "declined", "failed", "unknown"]);
      expect(options.map((o) => o.textContent)).toEqual([
        "All",
        "Created",
        "Succeeded",
        "Declined",
        "Failed",
        "Unknown",
      ]);
    });
  });

  describe("when the list request fails", () => {
    it("shows the error with a Retry control instead of Loading", async () => {
      // Arrange
      setup(() => jsonResponse(422, { detail: [{ loc: ["query", "state"], msg: "bad" }] }));

      // Act
      const alert = await within(paymentsSection()).findByRole("alert");

      // Assert
      expect(alert.textContent).not.toBe("");
      expect(within(paymentsSection()).queryByText("Loading…")).toBeNull();
      expect(within(paymentsSection()).getByRole("button", { name: "Retry" }).textContent).toBe("Retry");
    });

    it("loads the list again when Retry is pressed", async () => {
      // Arrange
      let calls = 0;
      const harness = setup(() => {
        calls += 1;
        return calls === 1
          ? jsonResponse(500, { detail: "internal error" })
          : jsonResponse(200, { items: [paymentOf("pay_1", "succeeded")], next_cursor: null });
      });
      const retry = await within(paymentsSection()).findByRole("button", { name: "Retry" });

      // Act
      fireEvent.click(retry);

      // Assert
      await waitFor(() => expect(paymentRowIds()).toEqual(["pay_1"]));
      expect(harness.sent("GET", PAYMENTS_PATH)).toHaveLength(2);
      expect(within(paymentsSection()).queryByRole("alert")).toBeNull();
    });
  });

  describe("while the first page is loading", () => {
    it("shows Loading and no error or table", async () => {
      // Arrange
      const pending = pendingResponse();
      setup(() => pending.promise);

      // Act
      const loading = await within(paymentsSection()).findByText("Loading…");

      // Assert
      expect(loading.textContent).toBe("Loading…");
      expect(within(paymentsSection()).queryByRole("alert")).toBeNull();
      expect(within(paymentsSection()).queryByRole("table")).toBeNull();

      await act(async () => {
        pending.resolve(jsonResponse(200, { items: [], next_cursor: null }));
      });
    });
  });

  describe("when the filter changes while an earlier request is slow", () => {
    it("keeps the list of the current filter when the earlier response arrives last", async () => {
      // Arrange
      const slowAll = pendingResponse();
      let calls = 0;
      const harness = setup(() => {
        calls += 1;
        return calls === 1
          ? slowAll.promise
          : jsonResponse(200, { items: [paymentOf("pay_declined", "declined")], next_cursor: null });
      });
      await waitFor(() => expect(harness.sent("GET", PAYMENTS_PATH)).toHaveLength(1));
      fireEvent.change(screen.getByLabelText("State"), { target: { value: "declined" } });
      await waitFor(() => expect(paymentRowIds()).toEqual(["pay_declined"]));

      // Act
      await act(async () => {
        slowAll.resolve(jsonResponse(200, { items: [paymentOf("pay_all", "succeeded")], next_cursor: null }));
      });

      // Assert
      expect(paymentRowIds()).toEqual(["pay_declined"]);
    });
  });

  describe("Load more", () => {
    it("is disabled while the next page is loading", async () => {
      // Arrange
      const nextPage = pendingResponse();
      let calls = 0;
      const harness = setup(() => {
        calls += 1;
        return calls === 1
          ? jsonResponse(200, { items: [paymentOf("pay_1", "succeeded")], next_cursor: "c2" })
          : nextPage.promise;
      });
      const loadMore = await screen.findByRole("button", { name: "Load more" });

      // Act
      fireEvent.click(loadMore);
      fireEvent.click(loadMore);

      // Assert
      await waitFor(() => expect(screen.getByRole("button", { name: "Load more" }).hasAttribute("disabled")).toBe(true));
      expect(harness.sent("GET", PAYMENTS_PATH)).toHaveLength(2);

      await act(async () => {
        nextPage.resolve(jsonResponse(200, { items: [], next_cursor: null }));
      });
    });

    it("does not repeat a row that the next page returns again", async () => {
      // Arrange
      let calls = 0;
      setup(() => {
        calls += 1;
        return calls === 1
          ? jsonResponse(200, { items: [paymentOf("pay_1", "succeeded")], next_cursor: "c2" })
          : jsonResponse(200, { items: [paymentOf("pay_1", "succeeded"), paymentOf("pay_2", "succeeded")], next_cursor: null });
      });
      const loadMore = await screen.findByRole("button", { name: "Load more" });

      // Act
      fireEvent.click(loadMore);

      // Assert
      await waitFor(() => expect(paymentRowIds()).toEqual(["pay_1", "pay_2"]));
    });
  });

  describe("table", () => {
    it("sits in a keyboard-focusable scroll container with a label", async () => {
      // Arrange
      setup(() => jsonResponse(200, { items: [paymentOf("pay_1", "succeeded")], next_cursor: null }));
      await waitFor(() => expect(paymentRowIds()).toEqual(["pay_1"]));

      // Act
      const container = within(paymentsSection()).getByRole("region", { name: "Payments table" });

      // Assert
      expect(container.getAttribute("tabindex")).toBe("0");
      expect(container.className).toBe("table-scroll");
      expect(within(container).getAllByRole("table")).toHaveLength(1);
    });
  });
});
