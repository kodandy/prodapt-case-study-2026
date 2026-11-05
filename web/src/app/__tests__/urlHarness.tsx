import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { vi } from "vitest";
import { createApiClient } from "../../lib/api";
import { createAuthStore, type AuthStore } from "../../lib/authStore";
import { ServicesProvider } from "../services";

/** Like the shared harness, but keeps the query string, which polling and filter tests need. */
export interface UrlRequest {
  method: string;
  /** The URL without its query string. */
  path: string;
  query: URLSearchParams;
  headers: Headers;
  body: unknown;
}

export type UrlResponder = (request: UrlRequest) => Response | Promise<Response>;

export interface UrlHarness {
  store: AuthStore;
  requests: UrlRequest[];
  renderUi(ui: ReactNode): void;
  /** Requests of one method and exact path, in the order they were sent. */
  sent(method: "GET" | "POST", path: string): UrlRequest[];
}

export function createUrlHarness(responder: UrlResponder): UrlHarness {
  const requests: UrlRequest[] = [];
  const fetchFake = vi.fn<typeof fetch>((input, init = {}) => {
    const [path = "", queryText = ""] = String(input).split("?");
    const request: UrlRequest = {
      method: init.method ?? "GET",
      path,
      query: new URLSearchParams(queryText),
      headers: new Headers(init.headers),
      body: typeof init.body === "string" ? (JSON.parse(init.body) as unknown) : null,
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

/** The element at an index; fails the test with a clear message instead of returning undefined. */
export function nth<T>(items: readonly T[], index: number): T {
  const item = items[index];
  if (item === undefined) throw new Error(`no element at index ${index} (length ${items.length})`);
  return item;
}
