import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { createHarness, jsonResponse, type Responder } from "../../../app/__tests__/harness";
import { KeyEntry } from "../KeyEntry";

const TOKENS_PATH = "/api/gw/v1/merchant-tokens";

function setup(responder: Responder) {
  const harness = createHarness(responder);
  harness.store.setUserSession("user-token");
  harness.renderUi(<KeyEntry />);
  return harness;
}

async function submitKey(key: string): Promise<void> {
  fireEvent.change(screen.getByLabelText("API key"), { target: { value: key } });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await screen.findByRole("alert");
}

describe("KeyEntry failure with a signed-in user", () => {
  afterEach(() => {
    cleanup();
  });

  it("keeps the user session and clears the merchant credentials when the key is rejected with a 401", async () => {
    // Arrange
    const harness = setup(() => jsonResponse(401, { detail: "invalid api key" }));

    // Act
    await submitKey("bad-key");

    // Assert
    expect(harness.sent("POST", TOKENS_PATH).length).toBeGreaterThan(0);
    expect(harness.store.get()).toEqual({ userSessionToken: "user-token", merchantApiKey: null, merchantOpsToken: null });
  });

  it("keeps the user session and clears the merchant credentials when the token exchange fails with a 500", async () => {
    // Arrange
    const harness = setup(() => jsonResponse(500, { detail: "internal error" }));

    // Act
    await submitKey("some-key");

    // Assert
    expect(harness.sent("POST", TOKENS_PATH).length).toBeGreaterThan(0);
    expect(harness.store.get()).toEqual({ userSessionToken: "user-token", merchantApiKey: null, merchantOpsToken: null });
  });
});
