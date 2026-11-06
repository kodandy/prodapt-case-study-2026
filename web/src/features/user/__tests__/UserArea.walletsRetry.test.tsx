import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { createHarness, jsonResponse, pendingResponse, type Responder } from "../../../app/__tests__/harness";
import { UserArea } from "../UserArea";

const WALLETS_PATH = "/api/gw/v1/me/wallets";
const HISTORY_PATH = "/api/gw/v1/me/transfers";
const WALLET = { account_id: "acc-1", currency: "USD", available_minor: 123456, held_minor: 0 };

function setup(wallets: Responder) {
  const harness = createHarness((request) => {
    if (request.method === "GET" && request.path === WALLETS_PATH) return wallets(request);
    if (request.method === "GET" && request.path === HISTORY_PATH) return jsonResponse(200, { items: [], next_cursor: null });
    return jsonResponse(404, { detail: "not found" });
  });
  harness.store.setUserSession("user-token");
  harness.renderUi(<UserArea />);
  return harness;
}

function walletsSection(): HTMLElement {
  return screen.getByRole("region", { name: "Wallets" });
}

describe("UserArea wallets", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows the error with a Retry control instead of Loading when wallets fail to load", async () => {
    // Arrange
    setup(() => jsonResponse(500, { detail: "internal error" }));

    // Act
    const alert = await within(walletsSection()).findByRole("alert");

    // Assert
    expect(alert.textContent).not.toBe("");
    expect(within(walletsSection()).queryByText("Loading…")).toBeNull();
    expect(within(walletsSection()).getByRole("button", { name: "Retry" }).textContent).toBe("Retry");
  });

  it("loads the wallets again when Retry is pressed", async () => {
    // Arrange
    let calls = 0;
    const harness = setup(() => {
      calls += 1;
      return calls === 1 ? jsonResponse(500, { detail: "internal error" }) : jsonResponse(200, { items: [WALLET] });
    });
    const retry = await within(walletsSection()).findByRole("button", { name: "Retry" });

    // Act
    fireEvent.click(retry);

    // Assert
    const balance = await within(walletsSection()).findByText("$1,234.56");
    expect(balance.textContent).toBe("$1,234.56");
    expect(harness.sent("GET", WALLETS_PATH)).toHaveLength(2);
    expect(within(walletsSection()).queryByRole("alert")).toBeNull();
  });

  it("shows only Loading while the wallets request is pending", async () => {
    // Arrange
    const pending = pendingResponse();
    setup(() => pending.promise);

    // Act
    const loading = await within(walletsSection()).findByText("Loading…");

    // Assert
    expect(loading.textContent).toBe("Loading…");
    expect(within(walletsSection()).queryByRole("alert")).toBeNull();
    expect(within(walletsSection()).queryByRole("button", { name: "Retry" })).toBeNull();

    await act(async () => {
      pending.resolve(jsonResponse(200, { items: [WALLET] }));
    });
    await waitFor(() => expect(within(walletsSection()).queryByText("Loading…")).toBeNull());
  });

  it("puts the wallets table in a keyboard-focusable scroll container with a label", async () => {
    // Arrange
    setup(() => jsonResponse(200, { items: [WALLET] }));
    await within(walletsSection()).findByText("$1,234.56");

    // Act
    const container = within(walletsSection()).getByRole("region", { name: "Wallets table" });

    // Assert
    expect(container.getAttribute("tabindex")).toBe("0");
    expect(within(container).getAllByRole("table")).toHaveLength(1);
  });
});
