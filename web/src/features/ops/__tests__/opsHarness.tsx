import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { vi } from "vitest";
import { ServicesProvider } from "../../../app/services";
import { createApiClient } from "../../../lib/api";
import { createAuthStore, type AuthStore } from "../../../lib/authStore";
import { createOpsApiClient, createOpsSessionStore, type OpsSessionStore } from "../../../lib/opsSession";
import { OpsServicesProvider } from "../opsServices";

export interface OpsRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  authorization: string | null;
  body: unknown;
}

export type OpsResponder = (request: OpsRequest) => Response | Promise<Response>;

export interface OpsHarness {
  store: AuthStore;
  opsStore: OpsSessionStore;
  requests: OpsRequest[];
  renderUi(ui: ReactNode): void;
  sent(method: "GET" | "POST", path: string): OpsRequest[];
}

/** One fake fetch behind both the merchant/user client and the ops client, as in the App. */
export function createOpsHarness(responder: OpsResponder): OpsHarness {
  const requests: OpsRequest[] = [];
  const fetchFake = vi.fn<typeof fetch>((input, init = {}) => {
    const [path = "", queryText = ""] = String(input).split("?");
    const request: OpsRequest = {
      method: init.method ?? "GET",
      path,
      query: new URLSearchParams(queryText),
      authorization: new Headers(init.headers).get("Authorization"),
      body: typeof init.body === "string" ? (JSON.parse(init.body) as unknown) : null,
    };
    requests.push(request);
    return Promise.resolve(responder(request));
  });
  const store = createAuthStore();
  const client = createApiClient({ fetch: fetchFake, store });
  const opsStore = createOpsSessionStore();
  const opsClient = createOpsApiClient({ fetch: fetchFake, store: opsStore });
  return {
    store,
    opsStore,
    requests,
    renderUi: (ui) => {
      render(
        <ServicesProvider services={{ store, client }}>
          <OpsServicesProvider services={{ store: opsStore, client: opsClient }}>{ui}</OpsServicesProvider>
        </ServicesProvider>,
      );
    },
    sent: (method, path) => requests.filter((r) => r.method === method && r.path === path),
  };
}
