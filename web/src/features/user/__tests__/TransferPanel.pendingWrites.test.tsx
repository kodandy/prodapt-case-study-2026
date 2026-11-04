import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createHarness,
  idempotencyKeysOf,
  jsonResponse,
  type Harness,
  type Responder,
} from "../../../app/__tests__/harness";
import { TransferPanel } from "../TransferPanel";

const TRANSFERS_PATH = "/api/gw/v1/me/transfers";

const SUCCESS_BODY = {
  transfer_id: "t-1",
  state: "completed",
  amount_minor: 1250,
  currency: "USD",
  sender_available_minor: 8750,
};

function setup(responder: Responder) {
  const harness = createHarness(responder);
  harness.store.setUserSession("user-token");
  harness.renderUi(<TransferPanel onTransferred={vi.fn<() => void>()} />);
  return harness;
}

function review(amountText = "12.50"): void {
  fireEvent.change(screen.getByLabelText("Recipient email"), { target: { value: "bob@example.com" } });
  fireEvent.change(screen.getByLabelText("Amount"), { target: { value: amountText } });
  fireEvent.click(screen.getByRole("button", { name: "Review" }));
}

function confirm(): void {
  fireEvent.click(screen.getByRole("button", { name: "Confirm transfer" }));
}

function cancel(): void {
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
}

async function untilRequestFinished(): Promise<void> {
  await waitFor(() => {
    const button = screen.queryByRole("button", { name: "Confirm transfer" });
    expect(button === null || !button.hasAttribute("disabled")).toBe(true);
  });
}

function postKeys(harness: Harness): string[] {
  return idempotencyKeysOf(harness.sent("POST", TRANSFERS_PATH));
}

describe("TransferPanel pending writes", () => {
  afterEach(() => {
    cleanup();
  });

  describe("when the outcome is unknown and the user cancels", () => {
    it("reuses the Idempotency-Key when the same transfer is reviewed and confirmed again", async () => {
      // Arrange
      const harness = setup(() => jsonResponse(504, { detail: "gateway timeout" }));
      review();
      confirm();
      await untilRequestFinished();
      cancel();

      // Act
      review();
      confirm();
      await untilRequestFinished();

      // Assert
      const keys = postKeys(harness);
      expect(keys.length).toBeGreaterThan(1);
      expect(keys[0]).not.toBe("");
      expect(new Set(keys).size).toBe(1);
    });

    it("uses a new Idempotency-Key when the amount differs from the unknown transfer", async () => {
      // Arrange
      const harness = setup(() => jsonResponse(504, { detail: "gateway timeout" }));
      review("12.50");
      confirm();
      await untilRequestFinished();
      const firstKeys = new Set(postKeys(harness));
      cancel();

      // Act
      review("13.00");
      confirm();
      await untilRequestFinished();

      // Assert
      const laterKeys = new Set(postKeys(harness).filter((key) => !firstKeys.has(key)));
      expect(firstKeys.size).toBe(1);
      expect([...firstKeys][0]).not.toBe("");
      expect(laterKeys.size).toBe(1);
      expect([...laterKeys][0]).not.toBe("");
    });
  });

  describe("when the transfer gets a final answer", () => {
    it("uses a new Idempotency-Key for an identical review after a 400 that followed an unknown outcome", async () => {
      // Arrange
      let calls = 0;
      const harness = setup(() => {
        calls += 1;
        // The first confirm (one request plus its one retry) is unknown; every later request is a final 400.
        return calls <= 2 ? jsonResponse(504, { detail: "gateway timeout" }) : jsonResponse(400, { error: "insufficient_funds" });
      });
      review();
      confirm();
      await untilRequestFinished();
      cancel();
      review();
      confirm();
      await untilRequestFinished();
      const settledKeys = new Set(postKeys(harness));

      // Act
      review();
      confirm();
      await untilRequestFinished();

      // Assert
      const laterKeys = new Set(postKeys(harness).filter((key) => !settledKeys.has(key)));
      expect(settledKeys.size).toBe(1);
      expect(laterKeys.size).toBe(1);
      expect([...laterKeys][0]).not.toBe("");
    });

    it("uses a new Idempotency-Key for an identical review after a success", async () => {
      // Arrange
      const harness = setup(() => jsonResponse(201, SUCCESS_BODY));
      review();
      confirm();
      await untilRequestFinished();
      const firstKeys = new Set(postKeys(harness));

      // Act
      review();
      confirm();
      await untilRequestFinished();

      // Assert
      const secondKeys = new Set(postKeys(harness).filter((key) => !firstKeys.has(key)));
      expect(firstKeys.size).toBe(1);
      expect(secondKeys.size).toBe(1);
      expect([...secondKeys][0]).not.toBe("");
    });
  });
});
