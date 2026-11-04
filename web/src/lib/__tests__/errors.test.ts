import { describe, expect, it } from "vitest";
import { GENERIC_ERROR_MESSAGE, errorCodeOf, messageFor } from "../errors";

describe("errorCodeOf", () => {
  it("picks the error field of a domain outcome", () => {
    // Arrange
    const body = { error: "insufficient_funds", available_minor: 5 };

    // Act
    const code = errorCodeOf(409, body);

    // Assert
    expect(code).toBe("insufficient_funds");
  });

  it("picks a string detail when there is no error field", () => {
    // Arrange
    const body = { detail: "mfa required" };

    // Act
    const code = errorCodeOf(401, body);

    // Assert
    expect(code).toBe("mfa required");
  });

  it("falls back to http_<status> for a list detail without an error field", () => {
    // Arrange
    const body = { detail: [{ loc: ["body", "amount_minor"], msg: "field required", type: "missing" }] };

    // Act
    const code = errorCodeOf(422, body);

    // Assert
    expect(code).toBe("http_422");
  });

  it("prefers the error field over a list detail", () => {
    // Arrange
    const body = { error: "validation_failed", detail: [{ loc: ["body"], msg: "bad", type: "value_error" }] };

    // Act
    const code = errorCodeOf(422, body);

    // Assert
    expect(code).toBe("validation_failed");
  });

  it("prefers the error field over a string detail", () => {
    // Arrange
    const body = { error: "recipient_not_found", detail: "other" };

    // Act
    const code = errorCodeOf(404, body);

    // Assert
    expect(code).toBe("recipient_not_found");
  });

  it("falls back to http_<status> for a null body", () => {
    // Arrange / Act
    const code = errorCodeOf(500, null);

    // Assert
    expect(code).toBe("http_500");
  });

  it("falls back to http_<status> for an undefined body", () => {
    // Arrange / Act
    const code = errorCodeOf(502, undefined);

    // Assert
    expect(code).toBe("http_502");
  });

  it("falls back to http_<status> for non-JSON text", () => {
    // Arrange
    const body = "<html>Internal Server Error</html>";

    // Act
    const code = errorCodeOf(500, body);

    // Assert
    expect(code).toBe("http_500");
  });

  it("falls back to http_<status> for an empty object", () => {
    // Arrange / Act
    const code = errorCodeOf(400, {});

    // Assert
    expect(code).toBe("http_400");
  });

  it("falls back to http_<status> when detail is an object", () => {
    // Arrange / Act
    const code = errorCodeOf(400, { detail: { nested: "x" } });

    // Assert
    expect(code).toBe("http_400");
  });
});

describe("messageFor", () => {
  const KNOWN_CODES = [
    "insufficient_funds",
    "recipient_not_found",
    "self_transfer",
    "idempotency_key_reused",
    "payment_not_refundable",
    "refund_exceeds_refundable",
    "payment_not_found",
    "email already registered",
    "invalid credentials",
    "mfa required",
    "mfa locked",
    "invalid code",
    "invalid api key",
    "totp already enabled",
    "network_error",
    "not_signed_in",
    "validation_failed",
  ] as const;

  it.each(KNOWN_CODES)("returns a specific message for %s", (code) => {
    // Arrange / Act
    const message = messageFor(code);

    // Assert
    expect(message).not.toBe(GENERIC_ERROR_MESSAGE);
    expect(message.length).toBeGreaterThan(0);
  });

  it("returns a distinct message for every known code", () => {
    // Arrange / Act
    const messages = KNOWN_CODES.map((code) => messageFor(code));

    // Assert
    expect(new Set(messages).size).toBe(KNOWN_CODES.length);
  });

  it("returns exactly the generic message for an unknown code", () => {
    // Arrange / Act
    const message = messageFor("totally_unknown_code");

    // Assert
    expect(message).toBe(GENERIC_ERROR_MESSAGE);
  });

  it("never echoes a hostile code", () => {
    // Arrange
    const hostile = "<script>x</script>";

    // Act
    const message = messageFor(hostile);

    // Assert
    expect(message).toBe(GENERIC_ERROR_MESSAGE);
  });

  it("returns the generic message for an empty code and an http_<status> code", () => {
    // Arrange / Act
    const empty = messageFor("");
    const http = messageFor("http_500");

    // Assert
    expect(empty).toBe(GENERIC_ERROR_MESSAGE);
    expect(http).toBe(GENERIC_ERROR_MESSAGE);
  });
});
