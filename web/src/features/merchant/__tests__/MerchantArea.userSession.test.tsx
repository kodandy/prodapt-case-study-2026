import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { createHarness, jsonResponse } from "../../../app/__tests__/harness";
import { MerchantArea } from "../MerchantArea";

const PAYMENTS_PATH = "/api/gw/v1/payments";

describe("MerchantArea Sign out with a signed-in user", () => {
  afterEach(() => {
    cleanup();
  });

  it("clears only the merchant credentials and keeps the user session", async () => {
    // Arrange
    const harness = createHarness(() => jsonResponse(200, { items: [], next_cursor: null }));
    harness.store.setUserSession("user-token");
    harness.store.setMerchant("merchant-key", "ops-token");
    harness.renderUi(<MerchantArea />);
    await waitFor(() => expect(harness.sent("GET", PAYMENTS_PATH)).toHaveLength(1));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    // Assert
    expect(harness.store.get()).toEqual({ userSessionToken: "user-token", merchantApiKey: null, merchantOpsToken: null });
    expect(screen.getByLabelText("API key").tagName).toBe("INPUT");
  });
});
