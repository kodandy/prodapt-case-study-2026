import { render } from "@testing-library/react";
import { vi } from "vitest";
import { createApiClient } from "../../lib/api";
import { createAuthStore } from "../../lib/authStore";
import { ServicesProvider } from "../services";
import type { Harness, RecordedRequest, Responder } from "./harness";

export interface QueryHarness extends Harness {
  /** Query strings of the requests of one method and exact path, in the order they were sent. */
  queriesOf(method: "GET" | "POST", path: string): URLSearchParams[];
}

interface QueryRecord {
  request: RecordedRequest;
  query: URLSearchParams;
}

function parseBody(raw: unknown): unknown {
  return typeof raw === "string" ? (JSON.parse(raw) as unknown) : null;
}

/**
 * Same wiring as createHarness (real store and client over a fake fetch), but it also keeps each request's
 * query string, which createHarness drops. Use it only where a test has to assert on query parameters.
 */
export function createQueryHarness(responder: Responder): QueryHarness {
  const records: QueryRecord[] = [];
  const fetchFake = vi.fn<typeof fetch>((input, init = {}) => {
    const [path = "", search = ""] = String(input).split("?");
    const request: RecordedRequest = {
      method: init.method ?? "GET",
      path,
      headers: new Headers(init.headers),
      body: parseBody(init.body),
    };
    records.push({ request, query: new URLSearchParams(search) });
    return Promise.resolve(responder(request));
  });
  const store = createAuthStore();
  const client = createApiClient({ fetch: fetchFake, store });
  const requests = {
    get all(): RecordedRequest[] {
      return records.map((r) => r.request);
    },
  };
  return {
    store,
    get requests() {
      return requests.all;
    },
    renderUi: (ui) => {
      render(<ServicesProvider services={{ store, client }}>{ui}</ServicesProvider>);
    },
    sent: (method, path) => requests.all.filter((r) => r.method === method && r.path === path),
    queriesOf: (method, path) =>
      records.filter((r) => r.request.method === method && r.request.path === path).map((r) => r.query),
  };
}
