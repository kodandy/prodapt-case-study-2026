import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createHarness,
  idempotencyKeysOf,
  jsonResponse,
  pendingResponse,
  type Harness,
  type RecordedRequest,
} from "../../../app/__tests__/harness";
import { useAuthState } from "../../../app/services";
import { PaymentDetail } from "../PaymentDetail";
import type { PaymentSummary } from "../types";

const REFUNDS_PATH = "/api/gw/v1/payments/pay_1/refunds";
const RISK_PATH = "/api/gw/v1/payments/pay_1/risk";

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
  amount_minor: 4000,
  currency: "USD",
  payment_refunded_minor: 5000,
  payment_refundable_minor: 0,
};

/** Remounts the detail whenever the merchant key changes, inside one provider so both merchants share the pending writes. */
function KeyedDetail(props: { onRefunded: () => void }): ReactNode {
  const { merchantApiKey } = useAuthState();
  return <PaymentDetail key={merchantApiKey ?? "signed-out"} payment={PAYMENT} onClose={vi.fn<() => void>()} onRefunded={props.onRefunded} />;
}

function review(): void {
  fireEvent.click(screen.getByRole("button", { name: "Review refund" }));
}

function confirm(): void {
  fireEvent.click(screen.getByRole("button", { name: "Confirm refund" }));
}

async function untilRequestFinished(): Promise<void> {
  await waitFor(() => {
    const button = screen.queryByRole("button", { name: "Confirm refund" });
    expect(button === null || !button.hasAttribute("disabled")).toBe(true);
  });
}

function postsWith(harness: Harness, apiKey: string): RecordedRequest[] {
  return harness.sent("POST", REFUNDS_PATH).filter((r) => r.headers.get("Authorization") === `Bearer ${apiKey}`);
}

describe("PaymentDetail stale settle", () => {
  afterEach(() => {
    cleanup();
  });

  it("keeps the second merchant key's unknown-outcome Idempotency-Key when the first key's late final answer settles the same refund", async () => {
    // Arrange
    const first = pendingResponse();
    const onRefunded = vi.fn<() => void>();
    const harness = createHarness((request) => {
      if (request.method === "GET" && request.path === RISK_PATH) {
        return jsonResponse(200, { payment_id: "pay_1", risk_flagged: false, blocked: false });
      }
      return request.headers.get("Authorization") === "Bearer key-a" ? first.promise : jsonResponse(504, { detail: "gateway timeout" });
    });
    harness.store.setMerchant("key-a", "ops-a");
    harness.renderUi(<KeyedDetail onRefunded={onRefunded} />);
    review();
    confirm();
    await waitFor(() => {
      expect(postsWith(harness, "key-a").length).toBe(1);
    });
    act(() => {
      harness.store.clear();
      harness.store.setMerchant("key-b", "ops-b");
    });
    review();
    confirm();
    await untilRequestFinished();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    // Act
    await act(async () => {
      first.resolve(jsonResponse(201, REFUND_BODY));
    });
    await waitFor(() => {
      expect(onRefunded).toHaveBeenCalledTimes(1);
    });
    review();
    confirm();
    await untilRequestFinished();

    // Assert
    const secondKeys = idempotencyKeysOf(postsWith(harness, "key-b"));
    const firstKeys = idempotencyKeysOf(postsWith(harness, "key-a"));
    expect(secondKeys.length).toBeGreaterThan(2);
    expect(secondKeys[0]).not.toBe("");
    expect(new Set(secondKeys).size).toBe(1);
    expect(firstKeys).toHaveLength(1);
    expect(firstKeys[0]).not.toBe("");
    expect(firstKeys[0]).not.toBe(secondKeys[0]);
  });
});
