import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { createHarness, jsonResponse, pendingResponse, type RecordedRequest } from "../../../app/__tests__/harness";
import { UserArea } from "../UserArea";

const WALLETS_PATH = "/api/gw/v1/me/wallets";
const HISTORY_PATH = "/api/gw/v1/me/transfers";

const TOKEN_A = "user-token-a";
const TOKEN_B = "user-token-b";
const BALANCE_A_TEXT = "$1,234.56";
const COUNTERPARTY_A = "alice-friend@example.com";

function bearerOf(request: RecordedRequest): string | null {
  return request.headers.get("Authorization");
}

describe("UserArea sign-out", () => {
  afterEach(() => {
    cleanup();
  });

  it("does not show the previous user's balance or history when another user signs in", async () => {
    // Arrange
    const walletsOfB = pendingResponse();
    const harness = createHarness((request) => {
      const asA = bearerOf(request) === `Bearer ${TOKEN_A}`;
      if (request.method === "GET" && request.path === WALLETS_PATH) {
        return asA
          ? jsonResponse(200, { items: [{ account_id: "acc-a", currency: "USD", available_minor: 123456, held_minor: 0 }] })
          : walletsOfB.promise;
      }
      if (request.method === "GET" && request.path === HISTORY_PATH) {
        return jsonResponse(200, {
          items: asA
            ? [
                {
                  transfer_id: "t-a",
                  direction: "sent",
                  counterparty_email_masked: COUNTERPARTY_A,
                  amount_minor: 500,
                  currency: "USD",
                  created_at: "2026-10-01T12:00:00Z",
                },
              ]
            : [],
          next_cursor: null,
        });
      }
      return jsonResponse(404, { detail: "not found" });
    });
    harness.store.setUserSession(TOKEN_A);
    harness.renderUi(<UserArea />);
    await screen.findByText(BALANCE_A_TEXT);
    await screen.findByText(COUNTERPARTY_A);

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    act(() => {
      harness.store.setUserSession(TOKEN_B);
    });
    await waitFor(() =>
      expect(
        harness.sent("GET", WALLETS_PATH).filter((request) => bearerOf(request) === `Bearer ${TOKEN_B}`),
      ).toHaveLength(1),
    );

    // Assert
    expect(document.body.textContent).not.toContain("1,234.56");
    expect(document.body.textContent).not.toContain(COUNTERPARTY_A);

    await act(async () => {
      walletsOfB.resolve(jsonResponse(200, { items: [] }));
    });
  });
});
