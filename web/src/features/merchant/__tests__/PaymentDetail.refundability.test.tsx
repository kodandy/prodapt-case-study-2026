import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createHarness, jsonResponse } from "../../../app/__tests__/harness";
import { PaymentDetail } from "../PaymentDetail";
import type { PaymentSummary } from "../types";

const REFUNDS_PATH = "/api/gw/v1/payments/pay_1/refunds";
const RISK_PATH = "/api/gw/v1/payments/pay_1/risk";

function paymentOf(state: string): PaymentSummary {
  return {
    payment_id: "pay_1",
    state,
    amount_minor: 1250,
    currency: "USD",
    method: "card",
    card: { bin: "411111", last4: "1111" },
    decline_code: state === "declined" ? "card_declined" : null,
    refunded_minor: 0,
    disputed_minor: 0,
    chargeback_minor: 0,
    created_at: "2026-10-01T12:00:00Z",
  };
}

function setup(payment: PaymentSummary, refundBody: unknown = null) {
  const harness = createHarness((request) => {
    if (request.method === "GET" && request.path === RISK_PATH) {
      return jsonResponse(200, { payment_id: "pay_1", risk_flagged: false, blocked: false });
    }
    if (request.method === "POST" && request.path === REFUNDS_PATH) return jsonResponse(201, refundBody);
    return jsonResponse(404, { detail: "not found" });
  });
  harness.store.setMerchant("merchant-api-key", "ops-token");
  harness.renderUi(<PaymentDetail payment={payment} onClose={vi.fn<() => void>()} onRefunded={vi.fn<() => void>()} />);
  return harness;
}

function refundAll(): void {
  fireEvent.click(screen.getByRole("button", { name: "Review refund" }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm refund" }));
}

describe("PaymentDetail refundability", () => {
  afterEach(() => {
    cleanup();
  });

  it.each(["declined", "failed", "created", "unknown", "not_an_api_state"])("offers no refund for a %s payment", (state) => {
    // Arrange
    setup(paymentOf(state));

    // Act
    const review = screen.queryByRole("button", { name: "Review refund" });

    // Assert
    expect(review).toBeNull();
    expect(screen.queryByLabelText("Refund amount (empty = full)")).toBeNull();
    expect(screen.getByTestId("refundable").textContent).toBe("$0.00");
  });

  it("offers a refund for a succeeded payment with an amount left", () => {
    // Arrange
    setup(paymentOf("succeeded"));

    // Act
    const review = screen.getByRole("button", { name: "Review refund" });

    // Assert
    expect(review.textContent).toBe("Review refund");
    expect(screen.getByTestId("refundable").textContent).toBe("$12.50");
  });

  describe("after a successful refund", () => {
    it("shows the refunded and refundable amounts from the refund response", async () => {
      // Arrange
      setup(paymentOf("succeeded"), {
        refund_id: "ref_1",
        payment_id: "pay_1",
        state: "succeeded",
        amount_minor: 1250,
        currency: "USD",
        payment_refunded_minor: 1250,
        payment_refundable_minor: 0,
      });

      // Act
      refundAll();

      // Assert
      await waitFor(() => expect(screen.getByTestId("refundable").textContent).toBe("$0.00"));
      expect(screen.getByTestId("refunded").textContent).toBe("$12.50");
      expect(screen.queryByRole("button", { name: "Review refund" })).toBeNull();
      expect(screen.getByText("Nothing left to refund.").textContent).toBe("Nothing left to refund.");
    });

    it("limits the next refund to the refundable amount from the response", async () => {
      // Arrange
      setup(paymentOf("succeeded"), {
        refund_id: "ref_1",
        payment_id: "pay_1",
        state: "succeeded",
        amount_minor: 1000,
        currency: "USD",
        payment_refunded_minor: 1000,
        payment_refundable_minor: 250,
      });
      fireEvent.change(screen.getByLabelText("Refund amount (empty = full)"), { target: { value: "10.00" } });
      refundAll();
      await waitFor(() => expect(screen.getByTestId("refundable").textContent).toBe("$2.50"));

      // Act
      fireEvent.change(screen.getByLabelText("Refund amount (empty = full)"), { target: { value: "10.00" } });
      fireEvent.click(screen.getByRole("button", { name: "Review refund" }));

      // Assert
      expect(screen.getByRole("status").textContent).toBe("At most $2.50 can be refunded.");
      expect(screen.queryByRole("button", { name: "Confirm refund" })).toBeNull();
    });
  });
});
