import { afterEach, beforeEach, describe, expect, it, vi, type Mock, type MockInstance } from "vitest";
import { createOpsApiClient, createOpsSessionStore, type OpsSessionState } from "../opsSession";

type FetchFn = typeof fetch;

const SIGNED_OUT: OpsSessionState = { sessionToken: null, role: null, opsAiToken: null };
const CREDENTIALS = { email: "agent@ops.example", password: "pw-secret", totpCode: "123456" };

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function setup() {
  const fetchMock = vi.fn<FetchFn>();
  const store = createOpsSessionStore();
  const client = createOpsApiClient({ fetch: fetchMock, store });
  return { fetchMock, store, client };
}

interface RecordedCall {
  url: string;
  method: string;
  authorization: string | null;
  body: unknown;
}

function callAt(fetchMock: Mock<FetchFn>, index: number): RecordedCall {
  const call = fetchMock.mock.calls[index];
  if (call === undefined) throw new Error(`fetch call #${index} was not made (calls: ${fetchMock.mock.calls.length})`);
  const [input, init = {}] = call;
  const headers = new Headers(init.headers);
  return {
    url: String(input),
    method: init.method ?? "GET",
    authorization: headers.get("Authorization"),
    body: typeof init.body === "string" ? (JSON.parse(init.body) as unknown) : null,
  };
}

function signedInResponses(fetchMock: Mock<FetchFn>): void {
  fetchMock
    .mockResolvedValueOnce(
      jsonResponse(201, { access_token: "ops-session-1", token_type: "bearer", expires_in: 900, role: "ops_agent" }),
    )
    .mockResolvedValueOnce(jsonResponse(201, { access_token: "ops-ai-1", expires_in: 900 }));
}

describe("createOpsSessionStore", () => {
  it("starts signed out", () => {
    // Arrange
    const store = createOpsSessionStore();

    // Act
    const state = store.get();

    // Assert
    expect(state).toEqual(SIGNED_OUT);
  });

  it("setSession stores the session and role and drops any earlier ops-ai token", () => {
    // Arrange
    const store = createOpsSessionStore();
    store.setSession("session-1", "ops_agent");
    store.setOpsAiToken("ops-ai-1");

    // Act
    store.setSession("session-2", "ops_lead");

    // Assert
    expect(store.get()).toEqual({ sessionToken: "session-2", role: "ops_lead", opsAiToken: null });
  });

  it("clear signs out and notifies subscribers", () => {
    // Arrange
    const store = createOpsSessionStore();
    store.setSession("session-1", "ops_agent");
    const listener = vi.fn<(state: OpsSessionState) => void>();
    store.subscribe(listener);

    // Act
    store.clear();

    // Assert
    expect(store.get()).toEqual(SIGNED_OUT);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledWith(SIGNED_OUT);
  });

  describe("memory-only guarantee", () => {
    let setItem: MockInstance;
    let cookieSetter: MockInstance;

    beforeEach(() => {
      setItem = vi.spyOn(Storage.prototype, "setItem");
      cookieSetter = vi.spyOn(document, "cookie", "set");
    });

    afterEach(() => {
      vi.restoreAllMocks();
    });

    it("never writes to Storage, document.cookie or the URL, and never logs a token", async () => {
      // Arrange
      const hrefBefore = window.location.href;
      const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map((level) =>
        vi.spyOn(console, level).mockImplementation(() => undefined),
      );
      const { fetchMock, store, client } = setup();
      signedInResponses(fetchMock);

      // Act
      await client.signIn(CREDENTIALS);
      store.clear();

      // Assert
      expect(setItem).toHaveBeenCalledTimes(0);
      expect(cookieSetter).toHaveBeenCalledTimes(0);
      expect(window.localStorage.length).toBe(0);
      expect(window.sessionStorage.length).toBe(0);
      expect(window.location.href).toBe(hrefBefore);
      for (const spy of consoleSpies) expect(spy).toHaveBeenCalledTimes(0);
    });
  });
});

describe("createOpsApiClient.signIn", () => {
  it("posts email, password and TOTP code to the gateway ops login, then gets the ops-ai token with the session", async () => {
    // Arrange
    const { fetchMock, store, client } = setup();
    signedInResponses(fetchMock);

    // Act
    const outcome = await client.signIn(CREDENTIALS);

    // Assert
    expect(outcome).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const login = callAt(fetchMock, 0);
    expect(login.url).toBe("/api/gw/v1/ops/sessions");
    expect(login.method).toBe("POST");
    expect(login.authorization).toBeNull();
    expect(login.body).toEqual({ email: "agent@ops.example", password: "pw-secret", totp_code: "123456" });
    const tokens = callAt(fetchMock, 1);
    expect(tokens.url).toBe("/api/gw/v1/ops/ops-ai-tokens");
    expect(tokens.method).toBe("POST");
    expect(tokens.authorization).toBe("Bearer ops-session-1");
    expect(store.get()).toEqual({ sessionToken: "ops-session-1", role: "ops_agent", opsAiToken: "ops-ai-1" });
  });

  it("trims the email but sends the password unchanged", async () => {
    // Arrange
    const { fetchMock, client } = setup();
    signedInResponses(fetchMock);

    // Act
    await client.signIn({ email: "  agent@ops.example ", password: " pw ", totpCode: "123456" });

    // Assert
    expect(callAt(fetchMock, 0).body).toEqual({ email: "agent@ops.example", password: " pw ", totp_code: "123456" });
  });

  it.each([
    ["an empty code", ""],
    ["five digits", "12345"],
    ["seven digits", "1234567"],
    ["letters", "12a456"],
  ])("refuses %s as the TOTP code without calling the server", async (_label, totpCode) => {
    // Arrange
    const { fetchMock, store, client } = setup();

    // Act
    const outcome = await client.signIn({ ...CREDENTIALS, totpCode });

    // Assert
    expect(outcome).toEqual({ ok: false, code: "totp_required" });
    expect(fetchMock).toHaveBeenCalledTimes(0);
    expect(store.get()).toEqual(SIGNED_OUT);
  });

  it("accepts a code typed with surrounding spaces", async () => {
    // Arrange
    const { fetchMock, client } = setup();
    signedInResponses(fetchMock);

    // Act
    const outcome = await client.signIn({ ...CREDENTIALS, totpCode: " 654321 " });

    // Assert
    expect(outcome).toEqual({ ok: true });
    expect(callAt(fetchMock, 0).body).toEqual({ email: "agent@ops.example", password: "pw-secret", totp_code: "654321" });
  });

  it.each([
    ["the email", { email: " " }],
    ["the password", { password: "" }],
  ])("refuses a missing value for %s without calling the server", async (_label, override) => {
    // Arrange
    const { fetchMock, client } = setup();

    // Act
    const outcome = await client.signIn({ ...CREDENTIALS, ...override });

    // Assert
    expect(outcome).toEqual({ ok: false, code: "missing_credentials" });
    expect(fetchMock).toHaveBeenCalledTimes(0);
  });

  it("returns the server's error code and stays signed out on bad credentials", async () => {
    // Arrange
    const { fetchMock, store, client } = setup();
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "invalid credentials" }));

    // Act
    const outcome = await client.signIn(CREDENTIALS);

    // Assert
    expect(outcome).toEqual({ ok: false, code: "invalid credentials" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(store.get()).toEqual(SIGNED_OUT);
  });

  it("returns the lockout code on 429", async () => {
    // Arrange
    const { fetchMock, store, client } = setup();
    fetchMock.mockResolvedValueOnce(jsonResponse(429, { detail: "too many attempts" }));

    // Act
    const outcome = await client.signIn(CREDENTIALS);

    // Assert
    expect(outcome).toEqual({ ok: false, code: "too many attempts" });
    expect(store.get()).toEqual(SIGNED_OUT);
  });

  it("signs out again when the ops-ai token cannot be issued", async () => {
    // Arrange
    const { fetchMock, store, client } = setup();
    fetchMock
      .mockResolvedValueOnce(jsonResponse(201, { access_token: "ops-session-1", token_type: "bearer", expires_in: 900, role: "ops_agent" }))
      .mockResolvedValueOnce(jsonResponse(503, { detail: "token issuing not configured" }));

    // Act
    const outcome = await client.signIn(CREDENTIALS);

    // Assert
    expect(outcome).toEqual({ ok: false, code: "token issuing not configured" });
    expect(store.get()).toEqual(SIGNED_OUT);
  });

  it("treats a network failure as an error and stays signed out", async () => {
    // Arrange
    const { fetchMock, store, client } = setup();
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    // Act
    const outcome = await client.signIn(CREDENTIALS);

    // Assert
    expect(outcome).toEqual({ ok: false, code: "network_error" });
    expect(store.get()).toEqual(SIGNED_OUT);
  });
});

describe("createOpsApiClient.request", () => {
  async function signedIn() {
    const harness = setup();
    signedInResponses(harness.fetchMock);
    await harness.client.signIn(CREDENTIALS);
    harness.fetchMock.mockReset();
    return harness;
  }

  it("sends the ops-ai token to the ops-ai service", async () => {
    // Arrange
    const { fetchMock, client } = await signedIn();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { server_time: "t", items: [] }));

    // Act
    const result = await client.request({ base: "ops", path: "/v1/ops/analytics/alerts", auth: "opsAi", query: { limit: 50 } });

    // Assert
    expect(result).toEqual({ ok: true, status: 200, data: { server_time: "t", items: [] } });
    const call = callAt(fetchMock, 0);
    expect(call.url).toBe("/api/ops/v1/ops/analytics/alerts?limit=50");
    expect(call.authorization).toBe("Bearer ops-ai-1");
  });

  it("answers not_signed_in without calling fetch while signed out", async () => {
    // Arrange
    const { fetchMock, client } = setup();

    // Act
    const result = await client.request({ base: "ops", path: "/v1/ops/analytics/alerts", auth: "opsAi" });

    // Assert
    expect(result).toEqual({ ok: false, status: 401, code: "not_signed_in", body: null });
    expect(fetchMock).toHaveBeenCalledTimes(0);
  });

  it("refreshes the ops-ai token once with the ops session on a 401 and repeats the request", async () => {
    // Arrange
    const { fetchMock, store, client } = await signedIn();
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { detail: "invalid token" }))
      .mockResolvedValueOnce(jsonResponse(201, { access_token: "ops-ai-2", expires_in: 900 }))
      .mockResolvedValueOnce(jsonResponse(200, { server_time: "t", items: [] }));

    // Act
    const result = await client.request({ base: "ops", path: "/v1/ops/analytics/alerts", auth: "opsAi" });

    // Assert
    expect(result.ok).toBe(true);
    expect(callAt(fetchMock, 1).url).toBe("/api/gw/v1/ops/ops-ai-tokens");
    expect(callAt(fetchMock, 1).authorization).toBe("Bearer ops-session-1");
    expect(callAt(fetchMock, 2).authorization).toBe("Bearer ops-ai-2");
    expect(store.get().opsAiToken).toBe("ops-ai-2");
  });

  it("signs out when the ops session itself has expired during a refresh", async () => {
    // Arrange
    const { fetchMock, store, client } = await signedIn();
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { detail: "invalid token" }))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "invalid ops session" }));

    // Act
    const result = await client.request({ base: "ops", path: "/v1/ops/analytics/alerts", auth: "opsAi" });

    // Assert
    expect(result).toEqual({ ok: false, status: 401, code: "invalid token", body: { detail: "invalid token" } });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(store.get()).toEqual(SIGNED_OUT);
  });

  it("keeps the session on a 403 (role outside the route policy)", async () => {
    // Arrange
    const { fetchMock, store, client } = await signedIn();
    fetchMock.mockResolvedValueOnce(jsonResponse(403, { detail: "forbidden" }));

    // Act
    const result = await client.request({ base: "ops", path: "/v1/ops/analytics/alerts", auth: "opsAi" });

    // Assert
    expect(result).toEqual({ ok: false, status: 403, code: "forbidden", body: { detail: "forbidden" } });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(store.get().sessionToken).toBe("ops-session-1");
  });

  it("shares one refresh between concurrent 401s", async () => {
    // Arrange
    const { fetchMock, client } = await signedIn();
    fetchMock.mockImplementation((input, init = {}) => {
      const auth = new Headers(init.headers).get("Authorization");
      if (String(input) === "/api/gw/v1/ops/ops-ai-tokens") {
        return Promise.resolve(jsonResponse(201, { access_token: "ops-ai-2", expires_in: 900 }));
      }
      if (auth === "Bearer ops-ai-1") return Promise.resolve(jsonResponse(401, { detail: "invalid token" }));
      return Promise.resolve(jsonResponse(200, { ok: 1 }));
    });

    // Act
    const results = await Promise.all([
      client.request({ base: "ops", path: "/v1/ops/analytics/forecast", auth: "opsAi" }),
      client.request({ base: "ops", path: "/v1/ops/analytics/chargeback-risk", auth: "opsAi" }),
    ]);

    // Assert
    expect(results.map((r) => r.ok)).toEqual([true, true]);
    const refreshes = fetchMock.mock.calls.filter(([input]) => String(input) === "/api/gw/v1/ops/ops-ai-tokens");
    expect(refreshes).toHaveLength(1);
  });

  it("signs out on a 401 from a gateway ops route called with the session", async () => {
    // Arrange
    const { fetchMock, store, client } = await signedIn();
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "invalid ops session" }));

    // Act
    const result = await client.request({ base: "gw", path: "/v1/ops/kyc-reviews", auth: "opsSession" });

    // Assert
    expect(result.ok).toBe(false);
    expect(store.get()).toEqual(SIGNED_OUT);
  });
});
