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
import { TransferPanel } from "../TransferPanel";

const TRANSFERS_PATH = "/api/gw/v1/me/transfers";

const SUCCESS_BODY = {
  transfer_id: "t-1",
  state: "completed",
  amount_minor: 1250,
  currency: "USD",
  sender_available_minor: 8750,
};

/** Remounts the panel whenever the session changes, inside one provider so Alice and Bob share the pending writes. */
function SessionKeyedPanel(props: { onTransferred: () => void }): ReactNode {
  const { userSessionToken } = useAuthState();
  return <TransferPanel key={userSessionToken ?? "signed-out"} onTransferred={props.onTransferred} />;
}

function review(): void {
  fireEvent.change(screen.getByLabelText("Recipient email"), { target: { value: "bob@example.com" } });
  fireEvent.change(screen.getByLabelText("Amount"), { target: { value: "12.50" } });
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

function postsWith(harness: Harness, token: string): RecordedRequest[] {
  return harness.sent("POST", TRANSFERS_PATH).filter((r) => r.headers.get("Authorization") === `Bearer ${token}`);
}

describe("TransferPanel stale settle", () => {
  afterEach(() => {
    cleanup();
  });

  it("keeps Bob's unknown-outcome key when Alice's late final answer settles the same body", async () => {
    // Arrange
    const alice = pendingResponse();
    const onTransferred = vi.fn<() => void>();
    const harness = createHarness((request) =>
      request.headers.get("Authorization") === "Bearer alice-token" ? alice.promise : jsonResponse(504, { detail: "gateway timeout" }),
    );
    harness.store.setUserSession("alice-token");
    harness.renderUi(<SessionKeyedPanel onTransferred={onTransferred} />);
    review();
    confirm();
    await waitFor(() => {
      expect(postsWith(harness, "alice-token").length).toBe(1);
    });
    act(() => {
      harness.store.clear();
      harness.store.setUserSession("bob-token");
    });
    review();
    confirm();
    await untilRequestFinished();
    cancel();

    // Act
    await act(async () => {
      alice.resolve(jsonResponse(201, SUCCESS_BODY));
    });
    await waitFor(() => {
      expect(onTransferred).toHaveBeenCalledTimes(1);
    });
    review();
    confirm();
    await untilRequestFinished();

    // Assert
    const bobKeys = idempotencyKeysOf(postsWith(harness, "bob-token"));
    const aliceKeys = idempotencyKeysOf(postsWith(harness, "alice-token"));
    expect(bobKeys.length).toBeGreaterThan(2);
    expect(bobKeys[0]).not.toBe("");
    expect(new Set(bobKeys).size).toBe(1);
    expect(aliceKeys).toHaveLength(1);
    expect(aliceKeys[0]).not.toBe("");
    expect(aliceKeys[0]).not.toBe(bobKeys[0]);
  });
});
