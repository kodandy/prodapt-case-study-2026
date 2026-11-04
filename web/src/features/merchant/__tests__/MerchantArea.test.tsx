import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { createHarness, jsonResponse, type RecordedRequest } from "../../../app/__tests__/harness";
import { MerchantArea } from "../MerchantArea";
import type { PaymentSummary } from "../types";

const PAYMENTS_PATH = "/api/gw/v1/payments";

const KEY_A = "merchant-key-a";
const KEY_B = "merchant-key-b";
const PAYMENT_ID_A = "pay_merchant_a_0001";

const PAYMENT_A: PaymentSummary = {
  payment_id: PAYMENT_ID_A,
  state: "captured",
  amount_minor: 5000,
  currency: "USD",
  method: "card",
  card: { bin: "411111", last4: "1111" },
  decline_code: null,
  refunded_minor: 0,
  disputed_minor: 0,
  chargeback_minor: 0,
  created_at: "2026-10-01T12:00:00Z",
};

function bearerOf(request: RecordedRequest): string | null {
  return request.headers.get("Authorization");
}

function paymentRowIds(): (string | null)[] {
  return screen.queryAllByTestId("payment-row").map((row) => row.getAttribute("data-payment-id"));
}

describe("MerchantArea sign-out", () => {
  afterEach(() => {
    cleanup();
  });

  it("does not show the previous merchant's payments when the next sign-in fails to load", async () => {
    // Arrange
    const harness = createHarness((request) => {
      const asA = bearerOf(request) === `Bearer ${KEY_A}`;
      if (request.method === "GET" && request.path === PAYMENTS_PATH) {
        return asA
          ? jsonResponse(200, { items: [PAYMENT_A], next_cursor: null })
          : jsonResponse(500, { detail: "internal error" });
      }
      if (request.method === "GET" && request.path === `${PAYMENTS_PATH}/${PAYMENT_ID_A}/risk`) {
        return jsonResponse(200, { payment_id: PAYMENT_ID_A, risk_flagged: false, blocked: false });
      }
      return jsonResponse(404, { detail: "not found" });
    });
    harness.store.setMerchant(KEY_A, "ops-token-a");
    harness.renderUi(<MerchantArea />);
    await waitFor(() => expect(paymentRowIds()).toEqual([PAYMENT_ID_A]));
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await screen.findByText(PAYMENT_ID_A);

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    act(() => {
      harness.store.setMerchant(KEY_B, "ops-token-b");
    });
    await screen.findByRole("alert");

    // Assert
    expect(
      harness.sent("GET", PAYMENTS_PATH).filter((request) => bearerOf(request) === `Bearer ${KEY_B}`),
    ).toHaveLength(1);
    expect(paymentRowIds()).toEqual([]);
    expect(document.body.textContent).not.toContain(PAYMENT_ID_A);
  });
});
