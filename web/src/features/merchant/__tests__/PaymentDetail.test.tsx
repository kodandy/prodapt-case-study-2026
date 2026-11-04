import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createHarness,
  idempotencyKeysOf,
  jsonResponse,
  networkError,
  pendingResponse,
  type Harness,
  type Responder,
} from "../../../app/__tests__/harness";
import { PaymentDetail } from "../PaymentDetail";
import type { PaymentSummary } from "../types";

const REFUNDS_PATH = "/api/gw/v1/payments/pay_1/refunds";
const RISK_PATH = "/api/gw/v1/payments/pay_1/risk";
const REFUNDABLE_MINOR = 4000;

const PAYMENT: PaymentSummary = {
  payment_id: "pay_1",
  state: "partially_refunded",
  amount_minor: 5000,
  currency: "USD",
  method: "card",
  card: { bin: "411111", last4: "1111" },
  decline_code: null,
  refunded_minor: 1000,
  disputed_minor: 0,
  chargeback_minor: 0,
  created_at: "2026-10-01T12:00:00Z",
};

const REFUND_BODY = {
  refund_id: "ref_1",
  payment_id: "pay_1",
  state: "succeeded",
  amount_minor: REFUNDABLE_MINOR,
  currency: "USD",
  payment_refunded_minor: 5000,
  payment_refundable_minor: 0,
};

function setup(refundResponder: Responder) {
  const harness = createHarness((request) => {
    if (request.method === "GET" && request.path === RISK_PATH) {
      return jsonResponse(200, { payment_id: "pay_1", risk_flagged: false, blocked: false });
    }
    return refundResponder(request);
  });
  harness.store.setMerchant("merchant-api-key", "ops-token");
  const onClose = vi.fn<() => void>();
  const onRefunded = vi.fn<() => void>();
  harness.renderUi(<PaymentDetail payment={PAYMENT} onClose={onClose} onRefunded={onRefunded} />);
  return { harness, onClose, onRefunded };
}

function isDisabled(element: HTMLElement): boolean {
  return element.hasAttribute("disabled");
}

function review(amountText: string): void {
  fireEvent.change(screen.getByLabelText("Refund amount (empty = full)"), { target: { value: amountText } });
  fireEvent.click(screen.getByRole("button", { name: "Review refund" }));
}

function confirm(): void {
  fireEvent.click(screen.getByRole("button", { name: "Confirm refund" }));
}

async function untilRequestFinished(): Promise<void> {
  await waitFor(() => {
    const button = screen.queryByRole("button", { name: "Confirm refund" });
    expect(button === null || !isDisabled(button)).toBe(true);
  });
}

function confirmGroups(): HTMLElement[] {
  return screen.queryAllByRole("group", { name: "Confirm refund" });
}

function refundKeys(harness: Harness): string[] {
  return idempotencyKeysOf(harness.sent("POST", REFUNDS_PATH));
}

const UNKNOWN_OUTCOMES: { name: string; respond: Responder }[] = [
  { name: "a network error", respond: () => networkError() },
  { name: "a 503 response", respond: () => jsonResponse(503, { detail: "unavailable" }) },
];

describe("PaymentDetail refund", () => {
  afterEach(() => {
    cleanup();
  });

  describe("when the outcome of the refund is unknown", () => {
    it.each(UNKNOWN_OUTCOMES)("keeps the confirm step after $name", async ({ respond }) => {
      // Arrange
      const { harness } = setup(respond);
      review("");

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      expect(confirmGroups()).toHaveLength(1);
      expect(harness.sent("POST", REFUNDS_PATH).length).toBeGreaterThan(0);
    });

    it.each(UNKNOWN_OUTCOMES)("retries with the same Idempotency-Key after $name", async ({ respond }) => {
      // Arrange
      const { harness } = setup(respond);
      review("");
      confirm();
      await untilRequestFinished();
      const postsAfterFirstConfirm = harness.sent("POST", REFUNDS_PATH).length;

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      const keys = refundKeys(harness);
      expect(keys.length).toBeGreaterThan(postsAfterFirstConfirm);
      expect(keys[0]).not.toBe("");
      expect(new Set(keys).size).toBe(1);
    });
  });

  describe("request body", () => {
    it("sends the refundable amount explicitly for a full refund", async () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(201, REFUND_BODY));
      review("");

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      const posts = harness.sent("POST", REFUNDS_PATH);
      expect(posts).toHaveLength(1);
      expect(posts[0]?.body).toEqual({ amount_minor: REFUNDABLE_MINOR });
    });

    it("sends the entered amount in minor units for a partial refund", async () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(201, { ...REFUND_BODY, amount_minor: 1250 }));
      review("12.50");

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      const posts = harness.sent("POST", REFUNDS_PATH);
      expect(posts).toHaveLength(1);
      expect(posts[0]?.body).toEqual({ amount_minor: 1250 });
    });
  });

  describe("when the server gives a final answer", () => {
    it("clears the draft after a 409 with another error code", async () => {
      // Arrange
      const { harness, onRefunded } = setup(() => jsonResponse(409, { error: "payment_not_refundable" }));
      review("");

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      expect(confirmGroups()).toHaveLength(0);
      expect(screen.getByRole("button", { name: "Review refund" }).textContent).toBe("Review refund");
      expect(harness.sent("POST", REFUNDS_PATH).length).toBeGreaterThan(0);
      expect(onRefunded).toHaveBeenCalledTimes(0);
    });

    it("uses a different Idempotency-Key for the next refund after a final 4xx", async () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(422, { error: "refund_exceeds_refundable" }));
      review("");
      confirm();
      await untilRequestFinished();
      const firstKeys = new Set(refundKeys(harness));

      // Act
      review("");
      confirm();
      await untilRequestFinished();

      // Assert
      const secondKeys = new Set(refundKeys(harness).filter((key) => !firstKeys.has(key)));
      expect(firstKeys.size).toBe(1);
      expect([...firstKeys][0]).not.toBe("");
      expect(secondKeys.size).toBe(1);
      expect([...secondKeys][0]).not.toBe("");
    });

    it("clears the draft and calls onRefunded once after success", async () => {
      // Arrange
      const { onRefunded } = setup(() => jsonResponse(201, REFUND_BODY));
      review("");

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      expect(confirmGroups()).toHaveLength(0);
      expect(onRefunded).toHaveBeenCalledTimes(1);
    });
  });

  describe("while the refund request is pending", () => {
    it("disables Cancel, Close and Confirm so the draft cannot be dropped mid-flight", async () => {
      // Arrange
      const pending = pendingResponse();
      const { harness, onClose } = setup(() => pending.promise);
      review("");

      // Act
      confirm();

      // Assert
      await waitFor(() => expect(harness.sent("POST", REFUNDS_PATH)).toHaveLength(1));
      expect(isDisabled(screen.getByRole("button", { name: "Cancel" }))).toBe(true);
      expect(isDisabled(screen.getByRole("button", { name: "Close" }))).toBe(true);
      expect(isDisabled(screen.getByRole("button", { name: "Confirm refund" }))).toBe(true);
      expect(onClose).toHaveBeenCalledTimes(0);

      await act(async () => {
        pending.resolve(jsonResponse(409, { error: "payment_not_refundable" }));
      });
    });
  });
});
