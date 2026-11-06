import { describe, expect, it, vi } from "vitest";
import { createOpsApiClient, createOpsSessionStore, type OpsSessionStore } from "../opsSession";

const ALERTS = { base: "ops", path: "/v1/ops/analytics/alerts", auth: "opsAi" } as const;
const ISSUE = { base: "gw", path: "/v1/ops/ops-ai-tokens", method: "POST", auth: "opsSession" } as const;
const TOKENS_URL = "/api/gw/v1/ops/ops-ai-tokens";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

interface Call {
  url: string;
  authorization: string | null;
}

interface Deferred {
  resolve(response: Response): void;
}

/** fetch whose answers the test hands out one call at a time, in the order the calls were made. */
function controlledFetch() {
  const calls: Call[] = [];
  const pending: Deferred[] = [];
  const fetchFn = vi.fn<typeof fetch>(
    (input, init = {}) =>
      new Promise<Response>((resolve) => {
        calls.push({ url: String(input).split("?")[0] ?? "", authorization: new Headers(init.headers).get("Authorization") });
        pending.push({ resolve });
      }),
  );
  async function answer(index: number, response: Response): Promise<void> {
    await vi.waitFor(() => {
      if (pending[index] === undefined) throw new Error(`fetch call #${index} not made yet`);
    });
    pending[index]?.resolve(response);
  }
  return { fetchFn, calls, answer };
}

function signedIn(session: string, opsAi: string): OpsSessionStore {
  const store = createOpsSessionStore();
  store.setSession(session, "ops_agent");
  store.setOpsAiToken(opsAi);
  return store;
}

function reSignIn(store: OpsSessionStore, session: string, opsAi: string): void {
  store.clear();
  store.setSession(session, "ops_lead");
  store.setOpsAiToken(opsAi);
}

describe("ops client: a late answer from an earlier session", () => {
  it("does not clear or refresh a newer session when an old ops-ai request answers 401", async () => {
    // Arrange
    const { fetchFn, calls, answer } = controlledFetch();
    const store = signedIn("session-1", "ai-1");
    const client = createOpsApiClient({ fetch: fetchFn, store });
    const inFlight = client.request(ALERTS);
    reSignIn(store, "session-2", "ai-2");

    // Act
    await answer(0, jsonResponse(401, { detail: "invalid token" }));
    const result = await inFlight;

    // Assert
    expect(result).toEqual({ ok: false, status: 401, code: "invalid token", body: { detail: "invalid token" } });
    expect(store.get()).toEqual({ sessionToken: "session-2", role: "ops_lead", opsAiToken: "ai-2" });
    expect(calls.map((c) => c.url)).toEqual(["/api/ops/v1/ops/analytics/alerts"]);
  });

  it("does not clear a newer session when an old session request answers 401", async () => {
    // Arrange
    const { fetchFn, answer } = controlledFetch();
    const store = signedIn("session-1", "ai-1");
    const client = createOpsApiClient({ fetch: fetchFn, store });
    const inFlight = client.request(ISSUE);
    reSignIn(store, "session-2", "ai-2");

    // Act
    await answer(0, jsonResponse(401, { detail: "invalid ops session" }));
    await inFlight;

    // Assert
    expect(store.get()).toEqual({ sessionToken: "session-2", role: "ops_lead", opsAiToken: "ai-2" });
  });

  it("does not clear a newer session when the old session's refresh answers 401", async () => {
    // Arrange
    const { fetchFn, calls, answer } = controlledFetch();
    const store = signedIn("session-1", "ai-1");
    const client = createOpsApiClient({ fetch: fetchFn, store });
    const inFlight = client.request(ALERTS);
    await answer(0, jsonResponse(401, { detail: "invalid token" }));
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    reSignIn(store, "session-2", "ai-2");

    // Act
    await answer(1, jsonResponse(401, { detail: "invalid ops session" }));
    await inFlight;

    // Assert
    expect(calls[1]).toEqual({ url: TOKENS_URL, authorization: "Bearer session-1" });
    expect(store.get()).toEqual({ sessionToken: "session-2", role: "ops_lead", opsAiToken: "ai-2" });
  });

  it("gives a newer session its own refresh instead of joining the old session's refresh", async () => {
    // Arrange
    const { fetchFn, calls, answer } = controlledFetch();
    const store = signedIn("session-1", "ai-1");
    const client = createOpsApiClient({ fetch: fetchFn, store });
    const oldRequest = client.request(ALERTS);
    await answer(0, jsonResponse(401, { detail: "invalid token" }));
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    reSignIn(store, "session-2", "ai-2");
    const newRequest = client.request(ALERTS);

    // Act
    await answer(2, jsonResponse(401, { detail: "invalid token" }));
    await vi.waitFor(() => expect(calls).toHaveLength(4));
    await answer(3, jsonResponse(201, { access_token: "ai-3", expires_in: 900 }));
    await answer(4, jsonResponse(200, { server_time: "t", items: [] }));
    const result = await newRequest;
    await answer(1, jsonResponse(201, { access_token: "ai-stale", expires_in: 900 }));
    await oldRequest;

    // Assert
    expect(calls[3]).toEqual({ url: TOKENS_URL, authorization: "Bearer session-2" });
    expect(calls[4]).toEqual({ url: "/api/ops/v1/ops/analytics/alerts", authorization: "Bearer ai-3" });
    expect(result).toEqual({ ok: true, status: 200, data: { server_time: "t", items: [] } });
    expect(store.get()).toEqual({ sessionToken: "session-2", role: "ops_lead", opsAiToken: "ai-3" });
  });
});

describe("ops client: a refresh that fails for a reason other than 401", () => {
  it.each([
    ["a 503", () => jsonResponse(503, { detail: "try again later" })],
    ["a 403", () => jsonResponse(403, { detail: "forbidden" })],
  ])("keeps the session and returns the original 401 after %s on refresh", async (_label, refreshAnswer) => {
    // Arrange
    const fetchFn = vi.fn<typeof fetch>((input) =>
      Promise.resolve(String(input) === TOKENS_URL ? refreshAnswer() : jsonResponse(401, { detail: "invalid token" })),
    );
    const store = signedIn("session-1", "ai-1");
    const client = createOpsApiClient({ fetch: fetchFn, store });

    // Act
    const result = await client.request(ALERTS);

    // Assert
    expect(result).toEqual({ ok: false, status: 401, code: "invalid token", body: { detail: "invalid token" } });
    expect(store.get()).toEqual({ sessionToken: "session-1", role: "ops_agent", opsAiToken: "ai-1" });
  });

  it("keeps the session and returns the original 401 when the refresh hits a network error", async () => {
    // Arrange
    const fetchFn = vi.fn<typeof fetch>((input) =>
      String(input) === TOKENS_URL
        ? Promise.reject(new TypeError("Failed to fetch"))
        : Promise.resolve(jsonResponse(401, { detail: "invalid token" })),
    );
    const store = signedIn("session-1", "ai-1");
    const client = createOpsApiClient({ fetch: fetchFn, store });

    // Act
    const result = await client.request(ALERTS);

    // Assert
    expect(result).toEqual({ ok: false, status: 401, code: "invalid token", body: { detail: "invalid token" } });
    expect(store.get().sessionToken).toBe("session-1");
  });

  it("signs out when the refresh itself answers 401", async () => {
    // Arrange
    const fetchFn = vi.fn<typeof fetch>((input) =>
      Promise.resolve(
        String(input) === TOKENS_URL
          ? jsonResponse(401, { detail: "invalid ops session" })
          : jsonResponse(401, { detail: "invalid token" }),
      ),
    );
    const store = signedIn("session-1", "ai-1");
    const client = createOpsApiClient({ fetch: fetchFn, store });

    // Act
    await client.request(ALERTS);

    // Assert
    expect(store.get()).toEqual({ sessionToken: null, role: null, opsAiToken: null });
  });
});

describe("ops client: credential and service pairing", () => {
  it("refuses to send the ops session to ops-ai", () => {
    // Arrange
    const fetchFn = vi.fn<typeof fetch>();
    const client = createOpsApiClient({ fetch: fetchFn, store: signedIn("session-1", "ai-1") });

    // Act
    // @ts-expect-error the type only allows the ops session with base "gw"
    const act = () => client.request({ base: "ops", path: "/v1/ops/analytics/alerts", auth: "opsSession" });

    // Assert
    expect(act).toThrow("ops credential opsSession is only sent to gw");
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("refuses to send the ops-ai token to the gateway", () => {
    // Arrange
    const fetchFn = vi.fn<typeof fetch>();
    const client = createOpsApiClient({ fetch: fetchFn, store: signedIn("session-1", "ai-1") });

    // Act
    // @ts-expect-error the type only allows the ops-ai token with base "ops"
    const act = () => client.request({ base: "gw", path: "/v1/ops/ops-ai-tokens", method: "POST", auth: "opsAi" });

    // Assert
    expect(act).toThrow("ops credential opsAi is only sent to ops");
    expect(fetchFn).not.toHaveBeenCalled();
  });
});
