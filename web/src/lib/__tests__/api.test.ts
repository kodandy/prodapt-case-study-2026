import { afterEach, beforeEach, describe, expect, it, vi, type Mock, type MockInstance } from "vitest";
import { createApiClient, newIdempotencyKey } from "../api";
import { createAuthStore, type AuthState } from "../authStore";

type FetchFn = typeof fetch;

const JSON_HEADERS = { "content-type": "application/json" };
const EMPTY: AuthState = { userSessionToken: null, merchantApiKey: null, merchantOpsToken: null };

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

function setup() {
  const fetchMock = vi.fn<FetchFn>();
  const store = createAuthStore();
  const client = createApiClient({ fetch: fetchMock, store });
  return { fetchMock, store, client };
}

interface RecordedCall {
  url: string;
  init: RequestInit;
  headers: Headers;
}

function callAt(fetchMock: Mock<FetchFn>, index: number): RecordedCall {
  const call = fetchMock.mock.calls[index];
  if (call === undefined) {
    throw new Error(`fetch call #${index} was not made (calls: ${fetchMock.mock.calls.length})`);
  }
  const [input, init = {}] = call;
  return { url: String(input), init, headers: new Headers(init.headers) };
}

describe("createApiClient", () => {
  describe("URL building", () => {
    it("prefixes the gateway path with /api/gw", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(200, { wallets: [] }));

      // Act
      await client.request({ base: "gw", path: "/v1/me/wallets", auth: "none" });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(callAt(fetchMock, 0).url).toBe("/api/gw/v1/me/wallets");
    });

    it("prefixes the ops-ai path with /api/ops", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

      // Act
      await client.request({ base: "ops", path: "/v1/assistant/ask", auth: "none" });

      // Assert
      expect(callAt(fetchMock, 0).url).toBe("/api/ops/v1/assistant/ask");
    });

    it("appends query parameters and drops undefined ones", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(200, { items: [] }));

      // Act
      await client.request({
        base: "gw",
        path: "/v1/payments",
        auth: "none",
        query: { limit: 20, cursor: undefined, state: "open" },
      });

      // Assert
      expect(callAt(fetchMock, 0).url).toBe("/api/gw/v1/payments?limit=20&state=open");
    });

    it("percent-encodes query values", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

      // Act
      await client.request({ base: "gw", path: "/v1/payments", auth: "none", query: { cursor: "a&b" } });

      // Assert
      expect(callAt(fetchMock, 0).url).toBe("/api/gw/v1/payments?cursor=a%26b");
    });

    it("adds no question mark when every query value is undefined", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

      // Act
      await client.request({ base: "gw", path: "/v1/payments", auth: "none", query: { cursor: undefined } });

      // Assert
      expect(callAt(fetchMock, 0).url).toBe("/api/gw/v1/payments");
    });
  });

  describe("Authorization header", () => {
    it("sends the user session as a Bearer token for auth user", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setUserSession("session-1");
      fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

      // Act
      await client.request({ base: "gw", path: "/v1/me/wallets", auth: "user" });

      // Assert
      expect(callAt(fetchMock, 0).headers.get("Authorization")).toBe("Bearer session-1");
    });

    it("sends the merchant API key as a Bearer token for auth merchantKey", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setMerchant("key-1", "ops-1");
      store.setUserSession("session-1");
      fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

      // Act
      await client.request({ base: "gw", path: "/v1/payments", auth: "merchantKey" });

      // Assert
      expect(callAt(fetchMock, 0).headers.get("Authorization")).toBe("Bearer key-1");
    });

    it("sends the ops token as a Bearer token for auth merchantOps", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setMerchant("key-1", "ops-1");
      fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

      // Act
      await client.request({ base: "ops", path: "/v1/assistant/ask", auth: "merchantOps" });

      // Assert
      expect(callAt(fetchMock, 0).headers.get("Authorization")).toBe("Bearer ops-1");
    });

    it("sends no Authorization header for auth none even when the store holds credentials", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setUserSession("session-1");
      store.setMerchant("key-1", "ops-1");
      fetchMock.mockResolvedValueOnce(jsonResponse(201, {}));

      // Act
      await client.request({ base: "gw", path: "/v1/sessions", method: "POST", auth: "none", body: { email: "a@b.c" } });

      // Assert
      expect(callAt(fetchMock, 0).headers.get("Authorization")).toBe(null);
    });
  });

  describe("method and body", () => {
    it("sends a POST with a JSON body and a content-type header", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(201, {}));
      const body = { email: "a@example.com", password: "pw", n: 5 };

      // Act
      await client.request({ base: "gw", path: "/v1/signup", method: "POST", auth: "none", body });

      // Assert
      const call = callAt(fetchMock, 0);
      expect(call.init.method).toBe("POST");
      expect(call.headers.get("Content-Type")).toBe("application/json");
      expect(typeof call.init.body).toBe("string");
      expect(JSON.parse(String(call.init.body))).toEqual(body);
    });

    it("sends a GET without a body", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

      // Act
      await client.request({ base: "gw", path: "/v1/me/wallets", auth: "none" });

      // Assert
      const call = callAt(fetchMock, 0);
      expect(call.init.method === undefined || call.init.method === "GET").toBe(true);
      expect(call.init.body).toBeUndefined();
    });
  });

  describe("results", () => {
    it("returns ok with status and parsed data on success", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(200, { wallets: [{ currency: "USD", available_minor: 5 }] }));

      // Act
      const result = await client.request({ base: "gw", path: "/v1/me/wallets", auth: "none" });

      // Assert
      expect(result).toEqual({ ok: true, status: 200, data: { wallets: [{ currency: "USD", available_minor: 5 }] } });
    });

    it("returns the status, the code from errorCodeOf and the body on an error response", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      const body = { error: "insufficient_funds", available_minor: 5 };
      fetchMock.mockResolvedValueOnce(jsonResponse(409, body));

      // Act
      const result = await client.request({ base: "gw", path: "/v1/me/transfers", method: "POST", auth: "none" });

      // Assert
      expect(result).toEqual({ ok: false, status: 409, code: "insufficient_funds", body });
    });

    it("uses a string detail as the code", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "mfa required" }));

      // Act
      const result = await client.request({ base: "gw", path: "/v1/sessions", method: "POST", auth: "none" });

      // Assert
      expect(result).toEqual({ ok: false, status: 401, code: "mfa required", body: { detail: "mfa required" } });
    });

    it("maps a non-JSON error response to http_<status>", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(new Response("Internal Server Error", { status: 500 }));

      // Act
      const result = await client.request({ base: "gw", path: "/v1/me/wallets", auth: "none" });

      // Assert
      expect(result).toMatchObject({ ok: false, status: 500, code: "http_500" });
    });
  });

  describe("idempotency and retries", () => {
    it("sends the Idempotency-Key header", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setUserSession("session-1");
      fetchMock.mockResolvedValueOnce(jsonResponse(201, {}));

      // Act
      await client.request({
        base: "gw",
        path: "/v1/me/transfers",
        method: "POST",
        auth: "user",
        body: {},
        idempotencyKey: "key-abc",
      });

      // Assert
      expect(callAt(fetchMock, 0).headers.get("Idempotency-Key")).toBe("key-abc");
    });

    it("sends no Idempotency-Key header when none is given", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

      // Act
      await client.request({ base: "gw", path: "/v1/me/wallets", auth: "none" });

      // Assert
      expect(callAt(fetchMock, 0).headers.get("Idempotency-Key")).toBe(null);
    });

    it.each([502, 503, 504])("retries once after a %i with the identical key and returns the second response", async (status) => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setUserSession("session-1");
      fetchMock.mockResolvedValueOnce(jsonResponse(status, { detail: "unavailable" }));
      fetchMock.mockResolvedValueOnce(jsonResponse(201, { transfer_id: "t-1" }));

      // Act
      const result = await client.request({
        base: "gw",
        path: "/v1/me/transfers",
        method: "POST",
        auth: "user",
        body: { amount_minor: 100 },
        idempotencyKey: "key-abc",
        retries: 1,
      });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(callAt(fetchMock, 0).headers.get("Idempotency-Key")).toBe("key-abc");
      expect(callAt(fetchMock, 1).headers.get("Idempotency-Key")).toBe("key-abc");
      expect(callAt(fetchMock, 1).headers.get("Authorization")).toBe("Bearer session-1");
      expect(result).toEqual({ ok: true, status: 201, data: { transfer_id: "t-1" } });
    });

    it("retries once after a network error with the identical key and returns the second response", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
      fetchMock.mockResolvedValueOnce(jsonResponse(201, { transfer_id: "t-1" }));

      // Act
      const result = await client.request({
        base: "gw",
        path: "/v1/me/transfers",
        method: "POST",
        auth: "none",
        body: {},
        idempotencyKey: "key-abc",
        retries: 1,
      });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(callAt(fetchMock, 0).headers.get("Idempotency-Key")).toBe("key-abc");
      expect(callAt(fetchMock, 1).headers.get("Idempotency-Key")).toBe("key-abc");
      expect(result).toEqual({ ok: true, status: 201, data: { transfer_id: "t-1" } });
    });

    it.each([400, 409, 422])("does not retry a %i", async (status) => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValue(jsonResponse(status, { error: "x" }));

      // Act
      const result = await client.request({
        base: "gw",
        path: "/v1/me/transfers",
        method: "POST",
        auth: "none",
        idempotencyKey: "key-abc",
        retries: 2,
      });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(result).toMatchObject({ ok: false, status, code: "x" });
    });

    it("does not retry a 503 when retries is 0", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(503, { detail: "down" }));

      // Act
      const result = await client.request({ base: "gw", path: "/v1/me/wallets", auth: "none", retries: 0 });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(result).toEqual({ ok: false, status: 503, code: "down", body: { detail: "down" } });
    });

    it("does not retry a 503 when retries is omitted", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValueOnce(jsonResponse(503, { detail: "down" }));

      // Act
      await client.request({ base: "gw", path: "/v1/me/wallets", auth: "none" });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it("returns the last response when every retry gets a 503", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockResolvedValue(jsonResponse(503, { detail: "down" }));
      fetchMock.mockResolvedValueOnce(jsonResponse(503, { detail: "down" }));

      // Act
      const result = await client.request({ base: "gw", path: "/v1/me/wallets", auth: "none", retries: 1 });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(result).toMatchObject({ ok: false, status: 503, code: "down" });
    });

    it("does not retry a network error when retries is 0", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

      // Act
      const result = await client.request({ base: "gw", path: "/v1/me/wallets", auth: "none", retries: 0 });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(result).toEqual({ ok: false, status: 0, code: "network_error", body: null });
    });

    it("resolves to network_error with status 0 once the retries are exhausted", async () => {
      // Arrange
      const { fetchMock, client } = setup();
      fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

      // Act
      const result = await client.request({ base: "gw", path: "/v1/me/wallets", auth: "none", retries: 1 });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(result).toMatchObject({ ok: false, status: 0, code: "network_error" });
    });
  });

  describe("missing credential", () => {
    it.each(["user", "merchantKey", "merchantOps"] as const)(
      "resolves to 401 not_signed_in without calling fetch for auth %s on an empty store",
      async (auth) => {
        // Arrange
        const { fetchMock, client } = setup();

        // Act
        const result = await client.request({ base: "gw", path: "/v1/me/wallets", auth });

        // Assert
        expect(fetchMock).toHaveBeenCalledTimes(0);
        expect(result).toMatchObject({ ok: false, status: 401, code: "not_signed_in" });
      },
    );

    it("does not use another credential in the store as a substitute", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setMerchant("key-1", "ops-1");

      // Act
      const result = await client.request({ base: "gw", path: "/v1/me/wallets", auth: "user" });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(0);
      expect(result).toMatchObject({ ok: false, status: 401, code: "not_signed_in" });
    });
  });

  describe("401 handling", () => {
    it("clears the whole store after a 401 on a user request", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setUserSession("session-1");
      store.setMerchant("key-1", "ops-1");
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "invalid credentials" }));

      // Act
      const result = await client.request({ base: "gw", path: "/v1/me/wallets", auth: "user" });

      // Assert
      expect(store.get()).toEqual(EMPTY);
      expect(result).toMatchObject({ ok: false, status: 401, code: "invalid credentials" });
    });

    it("clears only the merchant credentials after a 401 on a merchantKey request", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setUserSession("session-1");
      store.setMerchant("key-1", "ops-1");
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "invalid api key" }));

      // Act
      const result = await client.request({ base: "gw", path: "/v1/payments", auth: "merchantKey" });

      // Assert
      expect(store.get()).toEqual({ ...EMPTY, userSessionToken: "session-1" });
      expect(result).toMatchObject({ ok: false, status: 401, code: "invalid api key" });
    });

    it("leaves the store untouched after a 401 on a request with auth none", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setUserSession("session-1");
      store.setMerchant("key-1", "ops-1");
      const before = store.get();
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "invalid credentials" }));

      // Act
      const result = await client.request({ base: "gw", path: "/v1/sessions", method: "POST", auth: "none" });

      // Assert
      expect(store.get()).toEqual(before);
      expect(store.get()).toEqual({
        userSessionToken: "session-1",
        merchantApiKey: "key-1",
        merchantOpsToken: "ops-1",
      });
      expect(result).toMatchObject({ ok: false, status: 401, code: "invalid credentials" });
    });

    it("leaves the store untouched after a non-401 error on a user request", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setUserSession("session-1");
      fetchMock.mockResolvedValueOnce(jsonResponse(403, { detail: "forbidden" }));

      // Act
      await client.request({ base: "gw", path: "/v1/me/wallets", auth: "user" });

      // Assert
      expect(store.get()).toEqual({ ...EMPTY, userSessionToken: "session-1" });
    });
  });

  describe("merchantOps refresh", () => {
    it("refreshes the ops token once on a 401, repeats the request with the new token and returns its result", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setMerchant("key-1", "ops-old");
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }));
      fetchMock.mockResolvedValueOnce(
        jsonResponse(201, { access_token: "ops-new", token_type: "bearer", expires_in: 900 }),
      );
      fetchMock.mockResolvedValueOnce(jsonResponse(200, { text: "answer" }));

      // Act
      const result = await client.request({
        base: "ops",
        path: "/v1/assistant/ask",
        method: "POST",
        auth: "merchantOps",
        body: { question: "why?" },
      });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(3);
      const original = callAt(fetchMock, 0);
      const refresh = callAt(fetchMock, 1);
      const repeated = callAt(fetchMock, 2);
      expect(original.url).toBe("/api/ops/v1/assistant/ask");
      expect(original.headers.get("Authorization")).toBe("Bearer ops-old");
      expect(refresh.url).toBe("/api/gw/v1/merchant-tokens");
      expect(refresh.init.method).toBe("POST");
      expect(refresh.headers.get("Authorization")).toBe("Bearer key-1");
      expect(repeated.url).toBe("/api/ops/v1/assistant/ask");
      expect(repeated.init.method).toBe("POST");
      expect(repeated.headers.get("Authorization")).toBe("Bearer ops-new");
      expect(JSON.parse(String(repeated.init.body))).toEqual({ question: "why?" });
      expect(store.get()).toEqual({ ...EMPTY, merchantApiKey: "key-1", merchantOpsToken: "ops-new" });
      expect(result).toEqual({ ok: true, status: 200, data: { text: "answer" } });
    });

    it("sends exactly one POST to /api/gw/v1/merchant-tokens", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setMerchant("key-1", "ops-old");
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }));
      fetchMock.mockResolvedValueOnce(jsonResponse(201, { access_token: "ops-new" }));
      fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

      // Act
      await client.request({ base: "ops", path: "/v1/assistant/ask", method: "POST", auth: "merchantOps" });

      // Assert
      const refreshCalls = fetchMock.mock.calls.filter(
        ([input, init]) => String(input) === "/api/gw/v1/merchant-tokens" && init?.method === "POST",
      );
      expect(refreshCalls).toHaveLength(1);
    });

    it("clears only the merchant credentials and returns 401 when the refresh itself gets a 401", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setUserSession("session-1");
      store.setMerchant("key-1", "ops-old");
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }));
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "invalid api key" }));

      // Act
      const result = await client.request({
        base: "ops",
        path: "/v1/assistant/ask",
        method: "POST",
        auth: "merchantOps",
      });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(store.get()).toEqual({ ...EMPTY, userSessionToken: "session-1" });
      expect(result).toMatchObject({ ok: false, status: 401 });
    });

    it("refreshes only once when the repeated request is also a 401, then clears only the merchant credentials", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setUserSession("session-1");
      store.setMerchant("key-1", "ops-old");
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }));
      fetchMock.mockResolvedValueOnce(jsonResponse(201, { access_token: "ops-new" }));
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }));

      // Act
      const result = await client.request({
        base: "ops",
        path: "/v1/assistant/ask",
        method: "POST",
        auth: "merchantOps",
      });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(3);
      expect(store.get()).toEqual({ ...EMPTY, userSessionToken: "session-1" });
      expect(result).toMatchObject({ ok: false, status: 401 });
    });

    it("does not refresh on a non-401 error", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setMerchant("key-1", "ops-1");
      fetchMock.mockResolvedValueOnce(jsonResponse(403, { detail: "forbidden" }));

      // Act
      const result = await client.request({ base: "ops", path: "/v1/assistant/ask", auth: "merchantOps" });

      // Assert
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(store.get()).toEqual({ ...EMPTY, merchantApiKey: "key-1", merchantOpsToken: "ops-1" });
      expect(result).toMatchObject({ ok: false, status: 403, code: "forbidden" });
    });
  });

  describe("no leaks", () => {
    let setItem: MockInstance;
    let cookieSetter: MockInstance;
    let consoleSpies: MockInstance[];

    beforeEach(() => {
      setItem = vi.spyOn(Storage.prototype, "setItem");
      cookieSetter = vi.spyOn(document, "cookie", "set");
      consoleSpies = (["log", "info", "debug", "warn", "error"] as const).map((method) =>
        vi.spyOn(console, method).mockImplementation(() => undefined),
      );
    });

    afterEach(() => {
      window.localStorage.clear();
      window.sessionStorage.clear();
    });

    it("never writes to storage, cookies or the console across success, error, refresh and 401 flows", async () => {
      // Arrange
      const { fetchMock, store, client } = setup();
      store.setUserSession("secret-session-token");
      store.setMerchant("secret-api-key", "secret-ops-token");
      fetchMock.mockResolvedValueOnce(jsonResponse(200, { ok: 1 }));
      fetchMock.mockResolvedValueOnce(jsonResponse(409, { error: "insufficient_funds" }));
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }));
      fetchMock.mockResolvedValueOnce(jsonResponse(201, { access_token: "secret-new-ops-token" }));
      fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));
      fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
      fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "expired" }));

      // Act
      await client.request({ base: "gw", path: "/v1/me/wallets", auth: "user" });
      await client.request({ base: "gw", path: "/v1/me/transfers", method: "POST", auth: "user", body: {} });
      await client.request({ base: "ops", path: "/v1/assistant/ask", method: "POST", auth: "merchantOps" });
      await client.request({ base: "gw", path: "/v1/me/wallets", auth: "user" });
      await client.request({ base: "gw", path: "/v1/me/wallets", auth: "user" });

      // Assert
      expect(store.get()).toEqual(EMPTY);
      expect(setItem).toHaveBeenCalledTimes(0);
      expect(cookieSetter).toHaveBeenCalledTimes(0);
      expect(window.localStorage.length).toBe(0);
      expect(window.sessionStorage.length).toBe(0);
      for (const spy of consoleSpies) {
        expect(spy).toHaveBeenCalledTimes(0);
      }
    });
  });
});

describe("newIdempotencyKey", () => {
  const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

  it("returns a UUID v4 string", () => {
    // Arrange / Act
    const key = newIdempotencyKey();

    // Assert
    expect(key).toMatch(UUID_V4);
  });

  it("returns a distinct key on every call", () => {
    // Arrange / Act
    const keys = Array.from({ length: 100 }, () => newIdempotencyKey());

    // Assert
    expect(new Set(keys).size).toBe(100);
    for (const key of keys) {
      expect(key).toMatch(UUID_V4);
    }
  });
});
