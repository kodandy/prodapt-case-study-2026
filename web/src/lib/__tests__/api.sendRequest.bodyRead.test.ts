import { describe, expect, it, vi } from "vitest";
import { sendRequest } from "../api";

/** A response whose body stream fails while it is read (connection reset mid-body). */
function brokenBodyResponse(status: number): Response {
  const response = new Response("{}", { status });
  vi.spyOn(response, "text").mockRejectedValue(new TypeError("body stream failed"));
  return response;
}

describe("sendRequest when the response body cannot be read", () => {
  it("resolves to network_error instead of rejecting", async () => {
    // Arrange
    const fetchFn = vi.fn<typeof fetch>().mockResolvedValue(brokenBodyResponse(200));

    // Act
    const result = await sendRequest(fetchFn, { base: "gw", path: "/v1/ops/sessions", method: "POST" }, null);

    // Assert
    expect(result).toEqual({ ok: false, status: 0, code: "network_error", body: null });
  });

  it("retries a failed body read like a network error when retries are allowed", async () => {
    // Arrange
    const fetchFn = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(brokenBodyResponse(200))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: 1 }), { status: 200 }));

    // Act
    const result = await sendRequest(fetchFn, { base: "ops", path: "/v1/x", retries: 1 }, "token-1");

    // Assert
    expect(result).toEqual({ ok: true, status: 200, data: { ok: 1 } });
    expect(fetchFn).toHaveBeenCalledTimes(2);
  });
});
