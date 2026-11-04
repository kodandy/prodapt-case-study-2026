import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  idempotencyKeysOf,
  jsonResponse,
  pendingResponse,
  type RecordedRequest,
  type Responder,
} from "../app/__tests__/harness";
import { App } from "../App";

const SESSIONS_PATH = "/api/gw/v1/sessions";
const TRANSFERS_PATH = "/api/gw/v1/me/transfers";
const WALLETS_PATH = "/api/gw/v1/me/wallets";

/** Replaces global fetch, which App's client calls at request time, with a recording fake. */
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

function respondExceptTransfers(request: RecordedRequest, transfers: () => Response | Promise<Response>): Response | Promise<Response> {
  if (request.method === "POST" && request.path === SESSIONS_PATH) {
    return jsonResponse(200, { access_token: "user-token", expires_in: 900, amr: ["pwd"] });
  }
  if (request.method === "POST" && request.path === TRANSFERS_PATH) return transfers();
  if (request.path === WALLETS_PATH) return jsonResponse(200, { items: [] });
  if (request.path === TRANSFERS_PATH) return jsonResponse(200, { items: [], next_cursor: null });
  return jsonResponse(404, { detail: "not found" });
}

function transferKeys(requests: RecordedRequest[]): string[] {
  return idempotencyKeysOf(requests.filter((r) => r.method === "POST" && r.path === TRANSFERS_PATH));
}

async function logIn(): Promise<void> {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "alice@example.com" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "correct horse" } });
  fireEvent.click(screen.getByRole("button", { name: "Log in" }));
  await screen.findByRole("button", { name: "Log out" });
}

function review(): void {
  fireEvent.change(screen.getByLabelText("Recipient email"), { target: { value: "bob@example.com" } });
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

describe("App pending writes", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("reuses the Idempotency-Key when the User tab is left and re-entered while a transfer is pending", async () => {
    // Arrange
    const pending = pendingResponse();
    const requests = stubFetch((request) => respondExceptTransfers(request, () => pending.promise));
    render(<App />);
    await logIn();
    review();
    confirm();
    await waitFor(() => expect(transferKeys(requests)).toHaveLength(1));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Merchant" }));
    fireEvent.click(screen.getByRole("button", { name: "User" }));
    await screen.findByRole("button", { name: "Review" });
    review();
    confirm();
    await waitFor(() => expect(transferKeys(requests)).toHaveLength(2));

    // Assert
    const keys = transferKeys(requests);
    expect(keys[0]).not.toBe("");
    expect(new Set(keys).size).toBe(1);

    await act(async () => {
      pending.resolve(jsonResponse(400, { error: "insufficient_funds" }));
    });
  });

  it("uses a new Idempotency-Key after log out and log in with the same inputs", async () => {
    // Arrange
    const requests = stubFetch((request) => respondExceptTransfers(request, () => jsonResponse(504, { detail: "gateway timeout" })));
    render(<App />);
    await logIn();
    review();
    confirm();
    await untilRequestFinished();
    const firstKeys = new Set(transferKeys(requests));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    await logIn();
    review();
    confirm();
    await untilRequestFinished();

    // Assert
    const laterKeys = new Set(transferKeys(requests).filter((key) => !firstKeys.has(key)));
    expect(firstKeys.size).toBe(1);
    expect([...firstKeys][0]).not.toBe("");
    expect(laterKeys.size).toBe(1);
    expect([...laterKeys][0]).not.toBe("");
  });
});
