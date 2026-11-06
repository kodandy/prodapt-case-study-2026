import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  createHarness,
  idempotencyKeysOf,
  jsonResponse,
  pendingResponse,
  type Harness,
  type RecordedRequest,
  type Responder,
} from "../../../app/__tests__/harness";
import { MerchantArea } from "../MerchantArea";
import type { PaymentSummary } from "../types";

const PAYMENTS_PATH = "/api/gw/v1/payments";
const ASSISTANT_PATH = "/api/ops/v1/assistant/investigate";

const ID_A = "00000000-0000-4000-8000-00000000000a";
const ID_B = "00000000-0000-4000-8000-00000000000b";

function paymentOf(id: string, amountMinor: number): PaymentSummary {
  return {
    payment_id: id,
    state: "succeeded",
    amount_minor: amountMinor,
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

const PAYMENT_A = paymentOf(ID_A, 5000);
const PAYMENT_B = paymentOf(ID_B, 7000);

const REFUND_FINAL_422 = { error: "refund_exceeds_refundable" };
const UNKNOWN_504 = { detail: "gateway timeout" };

function refundsPath(id: string): string {
  return `${PAYMENTS_PATH}/${id}/refunds`;
}

function refundRequestsOf(harness: Harness, id: string): RecordedRequest[] {
  return harness.sent("POST", refundsPath(id));
}

function refundKeysOf(harness: Harness, id: string): string[] {
  return idempotencyKeysOf(refundRequestsOf(harness, id));
}

function setup(refunds: Responder, payments: PaymentSummary[] = [PAYMENT_A]) {
  const harness = createHarness((request) => {
    if (request.method === "GET" && request.path === PAYMENTS_PATH) {
      return jsonResponse(200, { items: payments, next_cursor: null });
    }
    if (request.method === "GET" && request.path.endsWith("/risk")) {
      const id = request.path.split("/").at(-2) ?? "";
      return jsonResponse(200, { payment_id: id, risk_flagged: false, blocked: false });
    }
    if (request.method === "POST" && request.path === ASSISTANT_PATH) {
      return jsonResponse(200, {
        mode: "t2_fallback",
        text: null,
        model: null,
        evidence: payments.map((p) => ({ ref: `payment:${p.payment_id}`, summary: "failed payment" })),
      });
    }
    return refunds(request);
  });
  harness.store.setMerchant("merchant-api-key", "ops-token");
  harness.renderUi(<MerchantArea />);
  return harness;
}

function rowOf(id: string): HTMLElement {
  const row = screen.getAllByTestId("payment-row").find((r) => r.getAttribute("data-payment-id") === id);
  if (row === undefined) throw new Error(`no row for ${id}`);
  return row;
}

async function openFromList(id: string): Promise<void> {
  await waitFor(() => expect(screen.queryAllByTestId("payment-row").length).toBeGreaterThan(0));
  fireEvent.click(within(rowOf(id)).getByRole("button", { name: "Open" }));
  await screen.findByRole("button", { name: "Review refund" });
}

function reviewRefund(amountText = ""): void {
  fireEvent.change(screen.getByLabelText("Refund amount (empty = full)"), { target: { value: amountText } });
  fireEvent.click(screen.getByRole("button", { name: "Review refund" }));
}

function confirmRefund(): void {
  fireEvent.click(screen.getByRole("button", { name: "Confirm refund" }));
}

async function untilRequestFinished(): Promise<void> {
  await waitFor(() => {
    const button = screen.queryByRole("button", { name: "Confirm refund" });
    expect(button === null || !button.hasAttribute("disabled")).toBe(true);
  });
}

async function resolvePending(pending: ReturnType<typeof pendingResponse>): Promise<void> {
  await act(async () => {
    pending.resolve(jsonResponse(422, REFUND_FINAL_422));
  });
}

describe("MerchantArea refund pending writes", () => {
  afterEach(() => {
    cleanup();
  });

  describe("while a refund is pending", () => {
    it("reuses the Idempotency-Key after the State filter is changed and the payment is reopened", async () => {
      // Arrange
      const pending = pendingResponse();
      const harness = setup(() => pending.promise);
      await openFromList(ID_A);
      reviewRefund();
      confirmRefund();
      await waitFor(() => expect(refundRequestsOf(harness, ID_A)).toHaveLength(1));

      // Act
      fireEvent.change(screen.getByLabelText("State"), { target: { value: "succeeded" } });
      await waitFor(() => expect(screen.queryAllByTestId("payment-row")).toHaveLength(1));
      fireEvent.change(screen.getByLabelText("State"), { target: { value: "" } });
      await waitFor(() => expect(screen.queryAllByTestId("payment-row")).toHaveLength(1));
      await openFromList(ID_A);
      reviewRefund();
      confirmRefund();
      await waitFor(() => expect(refundRequestsOf(harness, ID_A)).toHaveLength(2));

      // Assert
      const keys = refundKeysOf(harness, ID_A);
      expect(keys[0]).not.toBe("");
      expect(new Set(keys).size).toBe(1);

      await resolvePending(pending);
    });

    it("reuses the Idempotency-Key after another payment is opened from the list and the first is reopened", async () => {
      // Arrange
      const pending = pendingResponse();
      const harness = setup(() => pending.promise, [PAYMENT_A, PAYMENT_B]);
      await openFromList(ID_A);
      reviewRefund();
      confirmRefund();
      await waitFor(() => expect(refundRequestsOf(harness, ID_A)).toHaveLength(1));

      // Act
      await openFromList(ID_B);
      await openFromList(ID_A);
      reviewRefund();
      confirmRefund();
      await waitFor(() => expect(refundRequestsOf(harness, ID_A)).toHaveLength(2));

      // Assert
      const keys = refundKeysOf(harness, ID_A);
      expect(keys[0]).not.toBe("");
      expect(new Set(keys).size).toBe(1);

      await resolvePending(pending);
    });

    it("reuses the Idempotency-Key after another payment is opened from the assistant and the first is reopened", async () => {
      // Arrange
      const pending = pendingResponse();
      const harness = setup(() => pending.promise, [PAYMENT_B, PAYMENT_A]);
      await openFromList(ID_A);
      reviewRefund();
      confirmRefund();
      await waitFor(() => expect(refundRequestsOf(harness, ID_A)).toHaveLength(1));
      fireEvent.change(screen.getByLabelText("Question"), { target: { value: "why did payments fail?" } });
      fireEvent.click(screen.getByRole("button", { name: "Ask" }));
      const openButtons = await screen.findAllByRole("button", { name: "Open payment" });
      expect(openButtons).toHaveLength(2);

      // Act
      fireEvent.click(openButtons[0] as HTMLElement);
      await waitFor(() => expect(screen.getByText(ID_B).tagName).toBe("CODE"));
      await screen.findByRole("button", { name: "Review refund" });
      fireEvent.click(screen.getAllByRole("button", { name: "Open payment" })[1] as HTMLElement);
      await waitFor(() => expect(screen.getAllByText(ID_A).some((el) => el.closest("section.detail") !== null)).toBe(true));
      reviewRefund();
      confirmRefund();
      await waitFor(() => expect(refundRequestsOf(harness, ID_A)).toHaveLength(2));

      // Assert
      const keys = refundKeysOf(harness, ID_A);
      expect(keys[0]).not.toBe("");
      expect(new Set(keys).size).toBe(1);

      await resolvePending(pending);
    });
  });

  describe("when the outcome is unknown and the detail is closed", () => {
    it("reuses the Idempotency-Key after Close, reopening the payment and reviewing the same amount", async () => {
      // Arrange
      const harness = setup(() => jsonResponse(504, UNKNOWN_504));
      await openFromList(ID_A);
      reviewRefund("12.50");
      confirmRefund();
      await untilRequestFinished();
      fireEvent.click(screen.getByRole("button", { name: "Close" }));

      // Act
      await openFromList(ID_A);
      reviewRefund("12.50");
      confirmRefund();
      await untilRequestFinished();

      // Assert
      const posts = refundRequestsOf(harness, ID_A);
      const keys = refundKeysOf(harness, ID_A);
      expect(posts.map((r) => r.body)).toEqual(posts.map(() => ({ amount_minor: 1250 })));
      expect(keys.length).toBeGreaterThan(2);
      expect(keys[0]).not.toBe("");
      expect(new Set(keys).size).toBe(1);
    });

    it("reuses the Idempotency-Key after Cancel and reviewing the same amount again", async () => {
      // Arrange
      const harness = setup(() => jsonResponse(504, UNKNOWN_504));
      await openFromList(ID_A);
      reviewRefund();
      confirmRefund();
      await untilRequestFinished();
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

      // Act
      reviewRefund();
      confirmRefund();
      await untilRequestFinished();

      // Assert
      const keys = refundKeysOf(harness, ID_A);
      expect(keys.length).toBeGreaterThan(2);
      expect(keys[0]).not.toBe("");
      expect(new Set(keys).size).toBe(1);
    });

    it("uses a new Idempotency-Key when the reopened refund has a different amount", async () => {
      // Arrange
      const harness = setup(() => jsonResponse(504, UNKNOWN_504));
      await openFromList(ID_A);
      reviewRefund("12.50");
      confirmRefund();
      await untilRequestFinished();
      const firstKeys = new Set(refundKeysOf(harness, ID_A));
      fireEvent.click(screen.getByRole("button", { name: "Close" }));

      // Act
      await openFromList(ID_A);
      reviewRefund("13.00");
      confirmRefund();
      await untilRequestFinished();

      // Assert
      const laterKeys = new Set(refundKeysOf(harness, ID_A).filter((key) => !firstKeys.has(key)));
      expect(firstKeys.size).toBe(1);
      expect([...firstKeys][0]).not.toBe("");
      expect(laterKeys.size).toBe(1);
      expect([...laterKeys][0]).not.toBe("");
    });

    it("uses a different key for the same amount on another payment", async () => {
      // Arrange
      const harness = setup(() => jsonResponse(504, UNKNOWN_504), [PAYMENT_A, PAYMENT_B]);
      await openFromList(ID_A);
      reviewRefund("12.50");
      confirmRefund();
      await untilRequestFinished();
      fireEvent.click(screen.getByRole("button", { name: "Close" }));

      // Act
      await openFromList(ID_B);
      reviewRefund("12.50");
      confirmRefund();
      await untilRequestFinished();

      // Assert
      const keysA = new Set(refundKeysOf(harness, ID_A));
      const keysB = new Set(refundKeysOf(harness, ID_B));
      expect(keysA.size).toBe(1);
      expect(keysB.size).toBe(1);
      expect([...keysB][0]).not.toBe("");
      expect([...keysB][0]).not.toBe([...keysA][0]);
    });
  });

  describe("when the refund gets a final answer", () => {
    it("uses a new Idempotency-Key for an identical review after a final 422 that followed an unknown outcome", async () => {
      // Arrange
      let calls = 0;
      const harness = setup(() => {
        calls += 1;
        // The first confirm (one request plus its one retry) is unknown; every later request is a final 422.
        return calls <= 2 ? jsonResponse(504, UNKNOWN_504) : jsonResponse(422, REFUND_FINAL_422);
      });
      await openFromList(ID_A);
      reviewRefund();
      confirmRefund();
      await untilRequestFinished();
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      reviewRefund();
      confirmRefund();
      await untilRequestFinished();
      const settledKeys = new Set(refundKeysOf(harness, ID_A));

      // Act
      reviewRefund();
      confirmRefund();
      await untilRequestFinished();

      // Assert
      const laterKeys = new Set(refundKeysOf(harness, ID_A).filter((key) => !settledKeys.has(key)));
      expect(settledKeys.size).toBe(1);
      expect(laterKeys.size).toBe(1);
      expect([...laterKeys][0]).not.toBe("");
    });
  });

  describe("when the merchant signs out", () => {
    it("uses a new Idempotency-Key after signing in again and reviewing the same refund", async () => {
      // Arrange
      const harness = setup(() => jsonResponse(504, UNKNOWN_504));
      await openFromList(ID_A);
      reviewRefund();
      confirmRefund();
      await untilRequestFinished();
      const firstKeys = new Set(refundKeysOf(harness, ID_A));

      // Act
      fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
      act(() => {
        harness.store.setMerchant("merchant-api-key", "ops-token");
      });
      await openFromList(ID_A);
      reviewRefund();
      confirmRefund();
      await untilRequestFinished();

      // Assert
      const laterKeys = new Set(refundKeysOf(harness, ID_A).filter((key) => !firstKeys.has(key)));
      expect(firstKeys.size).toBe(1);
      expect([...firstKeys][0]).not.toBe("");
      expect(laterKeys.size).toBe(1);
      expect([...laterKeys][0]).not.toBe("");
    });
  });
});
