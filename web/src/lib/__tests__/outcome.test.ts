import { describe, expect, it } from "vitest";
import type { ApiResult } from "../api";
import { isOutcomeUnknown } from "../outcome";

function failure(status: number, code: string): ApiResult<never> {
  return { ok: false, status, code, body: null };
}

describe("isOutcomeUnknown", () => {
  describe("when the server may have applied the write", () => {
    it.each([
      { name: "network error", status: 0, code: "network_error" },
      { name: "request timeout", status: 408, code: "http_408" },
      { name: "too many requests", status: 429, code: "http_429" },
      { name: "internal server error", status: 500, code: "http_500" },
      { name: "bad gateway", status: 502, code: "http_502" },
      { name: "service unavailable", status: 503, code: "http_503" },
      { name: "gateway timeout", status: 504, code: "http_504" },
      { name: "highest 5xx status", status: 599, code: "http_599" },
    ])("returns true for $name (status $status)", ({ status, code }) => {
      // Arrange
      const result = failure(status, code);

      // Act
      const unknown = isOutcomeUnknown(result);

      // Assert
      expect(unknown).toBe(true);
    });

    it("returns true for the 409 idempotency_in_progress code", () => {
      // Arrange
      const result = failure(409, "idempotency_in_progress");

      // Act
      const unknown = isOutcomeUnknown(result);

      // Assert
      expect(unknown).toBe(true);
    });
  });

  describe("when the server gave a final answer", () => {
    it.each([
      { name: "bad request", status: 400, code: "insufficient_funds" },
      { name: "unauthorized", status: 401, code: "not_signed_in" },
      { name: "forbidden", status: 403, code: "http_403" },
      { name: "not found", status: 404, code: "payment_not_found" },
      { name: "conflict with another code", status: 409, code: "payment_not_refundable" },
      { name: "conflict that reuses a key", status: 409, code: "idempotency_key_reused" },
      { name: "unprocessable entity", status: 422, code: "validation_failed" },
      { name: "highest 4xx before 5xx", status: 499, code: "http_499" },
    ])("returns false for $name (status $status)", ({ status, code }) => {
      // Arrange
      const result = failure(status, code);

      // Act
      const unknown = isOutcomeUnknown(result);

      // Assert
      expect(unknown).toBe(false);
    });

    it.each([
      { status: 200, data: { transfer_id: "t-1" } },
      { status: 201, data: { refund_id: "r-1" } },
      { status: 204, data: null },
    ])("returns false for an ok result with status $status", ({ status, data }) => {
      // Arrange
      const result: ApiResult<unknown> = { ok: true, status, data };

      // Act
      const unknown = isOutcomeUnknown(result);

      // Assert
      expect(unknown).toBe(false);
    });
  });
});
