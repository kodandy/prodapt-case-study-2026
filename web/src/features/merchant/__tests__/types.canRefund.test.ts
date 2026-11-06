import { describe, expect, it } from "vitest";
import { canRefund, PAYMENT_STATE_FILTERS } from "../types";

describe("canRefund", () => {
  it("allows a refund only for succeeded among the API states", () => {
    // Arrange
    const apiStates = PAYMENT_STATE_FILTERS.map((s) => s.value);

    // Act
    const refundable = apiStates.filter((state) => canRefund(state));

    // Assert
    expect(apiStates).toEqual(["created", "succeeded", "declined", "failed", "unknown"]);
    expect(refundable).toEqual(["succeeded"]);
  });

  it.each(["", "SUCCEEDED", "refunded", "disputed", "authorized", "not_an_api_state"])(
    "refuses a refund for the unrecognised state %j",
    (state) => {
      // Arrange
      const input = state;

      // Act
      const allowed = canRefund(input);

      // Assert
      expect(allowed).toBe(false);
    },
  );
});
