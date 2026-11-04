import { expect, test } from "@playwright/test";

const CSP_DIRECTIVES = [
  "default-src 'self'",
  "script-src 'self'",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'none'",
];

test.describe("nginx security headers", () => {
  test("the SPA shell carries CSP, nosniff and no-cache", async ({ request }) => {
    // Arrange
    const path = "/";

    // Act
    const response = await request.get(path);

    // Assert
    const headers = response.headers();
    expect(response.status()).toBe(200);
    for (const directive of CSP_DIRECTIVES) {
      expect(headers["content-security-policy"]).toContain(directive);
    }
    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["referrer-policy"]).toBe("no-referrer");
    expect(headers["cache-control"]).toBe("no-cache");
  });

  for (const path of ["/api/gw/healthz", "/api/gw/readyz", "/api/ops/healthz"]) {
    test(`${path} is proxied with CSP, nosniff and no-store`, async ({ request }) => {
      // Arrange: the health endpoints need no credentials

      // Act
      const response = await request.get(path);

      // Assert
      const headers = response.headers();
      expect(response.status()).toBe(200);
      expect(headers["cache-control"]).toBe("no-store");
      expect(headers["x-content-type-options"]).toBe("nosniff");
      for (const directive of CSP_DIRECTIVES) {
        expect(headers["content-security-policy"]).toContain(directive);
      }
    });
  }

  test("an unauthenticated /api/gw/v1/me/wallets answers 401 with no-store", async ({ request }) => {
    // Arrange
    const path = "/api/gw/v1/me/wallets";

    // Act
    const response = await request.get(path);

    // Assert
    const headers = response.headers();
    expect(response.status()).toBe(401);
    expect(headers["cache-control"]).toBe("no-store");
    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["content-security-policy"]).toContain("default-src 'self'");
  });
});
