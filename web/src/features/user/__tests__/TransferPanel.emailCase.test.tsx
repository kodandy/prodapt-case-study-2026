import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createHarness, idempotencyKeysOf, jsonResponse, type Harness } from "../../../app/__tests__/harness";
import { TransferPanel } from "../TransferPanel";

const TRANSFERS_PATH = "/api/gw/v1/me/transfers";

function setup() {
  const harness = createHarness(() => jsonResponse(504, { detail: "gateway timeout" }));
  harness.store.setUserSession("user-token");
  harness.renderUi(<TransferPanel onTransferred={vi.fn<() => void>()} />);
  return harness;
}

function review(recipient: string): void {
  fireEvent.change(screen.getByLabelText("Recipient email"), { target: { value: recipient } });
  fireEvent.change(screen.getByLabelText("Amount"), { target: { value: "12.50" } });
  fireEvent.click(screen.getByRole("button", { name: "Review" }));
}

function confirm(): void {
  fireEvent.click(screen.getByRole("button", { name: "Confirm transfer" }));
}

async function untilRequestFinished(): Promise<void> {
  await waitFor(() => {
    const button = screen.queryByRole("button", { name: "Confirm transfer" });
    expect(button === null || !button.hasAttribute("disabled")).toBe(true);
  });
}

function posts(harness: Harness) {
  return harness.sent("POST", TRANSFERS_PATH);
}

describe("TransferPanel recipient email normalisation", () => {
  afterEach(() => {
    cleanup();
  });

  it("sends recipient_email trimmed and lowercased", async () => {
    // Arrange
    const harness = setup();
    review(" Bob@Example.COM ");

    // Act
    confirm();
    await untilRequestFinished();

    // Assert
    const bodies = posts(harness).map((r) => r.body);
    expect(bodies.length).toBeGreaterThan(0);
    for (const body of bodies) {
      expect(body).toEqual({ recipient_email: "bob@example.com", amount_minor: 1250, currency: "USD" });
    }
  });

  it("reuses one Idempotency-Key when the same recipient is re-entered in different case after an unknown outcome", async () => {
    // Arrange
    const harness = setup();
    review(" Bob@Example.COM ");
    confirm();
    await untilRequestFinished();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    // Act
    review("bob@example.com");
    confirm();
    await untilRequestFinished();

    // Assert
    const keys = idempotencyKeysOf(posts(harness));
    expect(keys.length).toBeGreaterThan(2);
    expect(keys[0]).not.toBe("");
    expect(new Set(keys).size).toBe(1);
  });
});
