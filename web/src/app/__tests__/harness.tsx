import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { vi } from "vitest";
import { createApiClient } from "../../lib/api";
import { createAuthStore, type AuthStore } from "../../lib/authStore";
import { ServicesProvider } from "../services";

export interface RecordedRequest {
  method: string;
  /** The URL without its query string. */
  path: string;
  headers: Headers;
  body: unknown;
}

export type Responder = (request: RecordedRequest) => Response | Promise<Response>;

export interface Harness {
  store: AuthStore;
  requests: RecordedRequest[];
  renderUi(ui: ReactNode): void;
  /** Requests of one method and exact path, in the order they were sent. */
  sent(method: "GET" | "POST", path: string): RecordedRequest[];
}

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** A fetch that resolves with a Response only when the test says so. */
export function pendingResponse(): { promise: Promise<Response>; resolve: (response: Response) => void } {
  let resolve: (response: Response) => void = () => undefined;
  const promise = new Promise<Response>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

export function networkError(): Promise<Response> {
  return Promise.reject(new TypeError("Failed to fetch"));
}

function parseBody(raw: unknown): unknown {
  return typeof raw === "string" ? (JSON.parse(raw) as unknown) : null;
}

/** Real auth store and API client over a fake fetch; the responder builds a fresh Response per call. */
export function createHarness(responder: Responder): Harness {
  const requests: RecordedRequest[] = [];
  const fetchFake = vi.fn<typeof fetch>((input, init = {}) => {
    const request: RecordedRequest = {
      method: init.method ?? "GET",
      path: String(input).split("?")[0] ?? "",
      headers: new Headers(init.headers),
      body: parseBody(init.body),
    };
    requests.push(request);
    return Promise.resolve(responder(request));
  });
  const store = createAuthStore();
  const client = createApiClient({ fetch: fetchFake, store });
  return {
    store,
    requests,
    renderUi: (ui) => {
      render(<ServicesProvider services={{ store, client }}>{ui}</ServicesProvider>);
    },
    sent: (method, path) => requests.filter((r) => r.method === method && r.path === path),
  };
}

export function idempotencyKeysOf(requests: RecordedRequest[]): string[] {
  return requests.map((r) => r.headers.get("Idempotency-Key") ?? "");
}
