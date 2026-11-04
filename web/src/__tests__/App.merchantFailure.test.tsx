import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { idempotencyKeysOf, jsonResponse, type RecordedRequest, type Responder } from "../app/__tests__/harness";
import { App } from "../App";

const SESSIONS_PATH = "/api/gw/v1/sessions";
const TRANSFERS_PATH = "/api/gw/v1/me/transfers";
const WALLETS_PATH = "/api/gw/v1/me/wallets";
const MERCHANT_TOKENS_PATH = "/api/gw/v1/merchant-tokens";

function stubFetch(responder: Responder): RecordedRequest[] {
  const requests: RecordedRequest[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>((input, init = {}) => {
      const request: RecordedRequest = {
        method: init.method ?? "GET",
        path: String(input).split("?")[0] ?? "",
        headers: new Headers(init.headers),
        body: typeof init.body === "string" ? (JSON.parse(init.body) as unknown) : null,
      };
      requests.push(request);
      return Promise.resolve(responder(request));
    }),
  );
  return requests;
}

function respond(request: RecordedRequest): Response {
  if (request.method === "POST" && request.path === SESSIONS_PATH) {
    return jsonResponse(200, { access_token: "user-token", expires_in: 900, amr: ["pwd"] });
  }
  if (request.method === "POST" && request.path === TRANSFERS_PATH) return jsonResponse(504, { detail: "gateway timeout" });
  if (request.method === "POST" && request.path === MERCHANT_TOKENS_PATH) return jsonResponse(401, { detail: "invalid api key" });
  if (request.path === WALLETS_PATH) return jsonResponse(200, { items: [] });
  if (request.path === TRANSFERS_PATH) return jsonResponse(200, { items: [], next_cursor: null });
  return jsonResponse(404, { detail: "not found" });
}

function transferKeys(requests: RecordedRequest[]): string[] {
  return idempotencyKeysOf(requests.filter((r) => r.method === "POST" && r.path === TRANSFERS_PATH));
}

function review(): void {
  fireEvent.change(screen.getByLabelText("Recipient email"), { target: { value: "bob@example.com" } });
  fireEvent.change(screen.getByLabelText("Amount"), { target: { value: "12.50" } });
  fireEvent.click(screen.getByRole("button", { name: "Review" }));
}

async function untilRequestFinished(): Promise<void> {
  await waitFor(() => {
    const button = screen.queryByRole("button", { name: "Confirm transfer" });
    expect(button === null || !button.hasAttribute("disabled")).toBe(true);
  });
}

describe("App merchant failure", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("keeps the user signed in and reuses the transfer's Idempotency-Key after a merchant key is rejected", async () => {
    // Arrange
    const requests = stubFetch(respond);
    render(<App />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "alice@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "correct horse" } });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));
    await screen.findByRole("button", { name: "Log out" });
    review();
    fireEvent.click(screen.getByRole("button", { name: "Confirm transfer" }));
    await untilRequestFinished();
    const firstKeys = transferKeys(requests);

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Merchant" }));
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "bad-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "User" }));
    await screen.findByRole("button", { name: "Review" });
    review();
    fireEvent.click(screen.getByRole("button", { name: "Confirm transfer" }));
    await waitFor(() => expect(transferKeys(requests).length).toBeGreaterThan(firstKeys.length));

    // Assert
    const keys = transferKeys(requests);
    expect(screen.getByRole("button", { name: "Log out" }).tagName).toBe("BUTTON");
    expect(screen.queryByRole("button", { name: "Log in" })).toBeNull();
    expect(firstKeys[0]).not.toBe("");
    expect(new Set(keys).size).toBe(1);
  });
});
