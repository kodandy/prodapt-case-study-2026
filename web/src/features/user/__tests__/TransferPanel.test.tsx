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
  const onTransferred = vi.fn<() => void>();
  harness.renderUi(<TransferPanel onTransferred={onTransferred} />);
  return { harness, onTransferred };
}

function isDisabled(element: HTMLElement): boolean {
  return element.hasAttribute("disabled");
}

function review(): void {
  fireEvent.change(screen.getByLabelText("Recipient email"), { target: { value: "bob@example.com" } });
  fireEvent.change(screen.getByLabelText("Amount"), { target: { value: "12.50" } });
  fireEvent.click(screen.getByRole("button", { name: "Review" }));
}

function confirm(): void {
  fireEvent.click(screen.getByRole("button", { name: "Confirm transfer" }));
}

/** Waits until the Confirm button is gone or usable again, i.e. the request finished. */
async function untilRequestFinished(): Promise<void> {
  await waitFor(() => {
    const button = screen.queryByRole("button", { name: "Confirm transfer" });
    expect(button === null || !isDisabled(button)).toBe(true);
  });
}

function confirmGroups(): HTMLElement[] {
  return screen.queryAllByRole("group", { name: "Confirm transfer" });
}

function postKeys(harness: Harness): string[] {
  return idempotencyKeysOf(harness.sent("POST", TRANSFERS_PATH));
}

const UNKNOWN_OUTCOMES: { name: string; respond: Responder }[] = [
  { name: "a network error", respond: () => networkError() },
  { name: "a 504 response", respond: () => jsonResponse(504, { detail: "gateway timeout" }) },
  { name: "an idempotency_in_progress response", respond: () => jsonResponse(409, { error: "idempotency_in_progress" }) },
];

describe("TransferPanel", () => {
  afterEach(() => {
    cleanup();
  });

  describe("when the outcome of the transfer is unknown", () => {
    it.each(UNKNOWN_OUTCOMES)("keeps the confirm step after $name", async ({ respond }) => {
      // Arrange
      const { harness } = setup(respond);
      review();

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      expect(confirmGroups()).toHaveLength(1);
      expect(harness.sent("POST", TRANSFERS_PATH).length).toBeGreaterThan(0);
    });

    it.each(UNKNOWN_OUTCOMES)("retries with the same Idempotency-Key after $name", async ({ respond }) => {
      // Arrange
      const { harness } = setup(respond);
      review();
      confirm();
      await untilRequestFinished();
      const postsAfterFirstConfirm = harness.sent("POST", TRANSFERS_PATH).length;

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      const keys = postKeys(harness);
      expect(keys.length).toBeGreaterThan(postsAfterFirstConfirm);
      expect(keys[0]).not.toBe("");
      expect(new Set(keys).size).toBe(1);
    });

    it("keeps the same request body on the retry", async () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(504, { detail: "gateway timeout" }));
      review();
      confirm();
      await untilRequestFinished();

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      const bodies = harness.sent("POST", TRANSFERS_PATH).map((r) => r.body);
      expect(bodies.length).toBeGreaterThan(1);
      for (const body of bodies) {
        expect(body).toEqual({ recipient_email: "bob@example.com", amount_minor: 1250, currency: "USD" });
      }
    });
  });

  describe("when the server gives a final answer", () => {
    it("clears the draft and returns to the form after a 400", async () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(400, { error: "insufficient_funds" }));
      review();

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      expect(confirmGroups()).toHaveLength(0);
      expect(screen.getByRole("button", { name: "Review" }).textContent).toBe("Review");
      expect(harness.sent("POST", TRANSFERS_PATH).length).toBeGreaterThan(0);
    });

    it("uses a different Idempotency-Key for the next review and confirm after a 400", async () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(400, { error: "insufficient_funds" }));
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
      expect([...firstKeys][0]).not.toBe("");
      expect(secondKeys.size).toBe(1);
      expect([...secondKeys][0]).not.toBe("");
    });

    it("clears the draft and calls onTransferred once after success", async () => {
      // Arrange
      const { harness, onTransferred } = setup(() => jsonResponse(201, SUCCESS_BODY));
      review();

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      expect(confirmGroups()).toHaveLength(0);
      expect(screen.getByRole("button", { name: "Review" }).textContent).toBe("Review");
      expect(onTransferred).toHaveBeenCalledTimes(1);
      expect(harness.sent("POST", TRANSFERS_PATH)).toHaveLength(1);
    });
  });

  describe("while the transfer request is pending", () => {
    it("disables Cancel so the draft cannot be dropped mid-flight", async () => {
      // Arrange
      const pending = pendingResponse();
      const { harness } = setup(() => pending.promise);
      review();

      // Act
      confirm();

      // Assert
      await waitFor(() => expect(harness.sent("POST", TRANSFERS_PATH)).toHaveLength(1));
      expect(isDisabled(screen.getByRole("button", { name: "Cancel" }))).toBe(true);
      expect(isDisabled(screen.getByRole("button", { name: "Confirm transfer" }))).toBe(true);

      await act(async () => {
        pending.resolve(jsonResponse(400, { error: "insufficient_funds" }));
      });
    });
  });
});
