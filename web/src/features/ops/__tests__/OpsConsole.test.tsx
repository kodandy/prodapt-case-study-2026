import { act, cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse } from "../../../app/__tests__/harness";
import { createUrlHarness, type UrlRequest } from "../../../app/__tests__/urlHarness";
import { OpsConsole } from "../OpsConsole";

function respond(request: UrlRequest): Response {
  if (request.path === "/api/ops/v1/analytics/alerts") {
    return jsonResponse(200, { server_time: "2026-11-05T10:00:00Z", items: [] });
  }
  if (request.path === "/api/ops/v1/tickets") return jsonResponse(200, { tickets: [] });
  return jsonResponse(404, { detail: "not found" });
}

describe("OpsConsole", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows the merchant sign-in and an ops note, and no panels, while signed out", () => {
    // Arrange
    const harness = createUrlHarness(respond);

    // Act
    harness.renderUi(<OpsConsole />);

    // Assert
    expect(screen.getByRole("heading", { name: "Merchant sign-in (demo)" }).textContent).toBe("Merchant sign-in (demo)");
    expect(document.body.textContent).toMatch(/\bops\b/i);
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Failure query" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Flagged transactions" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Live anomaly alerts" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Ticket queue" })).toBeNull();
    expect(harness.requests).toHaveLength(0);
  });

  it("shows a banner that the merchant token has no ops role once signed in", async () => {
    // Arrange
    const harness = createUrlHarness(respond);
    harness.store.setMerchant("merchant-key", "ops-token");

    // Act
    harness.renderUi(<OpsConsole />);
    await screen.findByText("No anomalies detected.");

    // Assert
    const banner = screen.getByRole("note");
    expect(banner.textContent).toContain("merchant token");
    expect(banner.textContent).toContain("no ops role");
  });

  it("shows the four panels once signed in", async () => {
    // Arrange
    const harness = createUrlHarness(respond);
    harness.store.setMerchant("merchant-key", "ops-token");

    // Act
    harness.renderUi(<OpsConsole />);
    await screen.findByText("No anomalies detected.");

    // Assert
    expect(screen.getByRole("heading", { name: "Failure query" }).textContent).toBe("Failure query");
    expect(screen.getByRole("heading", { name: "Flagged transactions" }).textContent).toBe("Flagged transactions");
    expect(screen.getByRole("heading", { name: "Live anomaly alerts" }).textContent).toBe("Live anomaly alerts");
    expect(screen.getByRole("heading", { name: "Ticket queue" }).textContent).toBe("Ticket queue");
    expect(screen.queryByRole("heading", { name: "Merchant sign-in (demo)" })).toBeNull();
  });

  it("loads the alerts and the tickets once on sign-in and nothing else", async () => {
    // Arrange
    const harness = createUrlHarness(respond);
    harness.store.setMerchant("merchant-key", "ops-token");

    // Act
    harness.renderUi(<OpsConsole />);
    await screen.findByText("No anomalies detected.");

    // Assert
    expect(harness.sent("GET", "/api/ops/v1/analytics/alerts")).toHaveLength(1);
    expect(harness.sent("GET", "/api/ops/v1/tickets")).toHaveLength(1);
    expect(harness.requests.filter((r) => r.method === "POST")).toHaveLength(0);
  });

  it("swaps the sign-in for the panels when credentials arrive", async () => {
    // Arrange
    const harness = createUrlHarness(respond);
    harness.renderUi(<OpsConsole />);

    // Act
    act(() => {
      harness.store.setMerchant("merchant-key", "ops-token");
    });

    // Assert
    expect((await screen.findByRole("heading", { name: "Ticket queue" })).textContent).toBe("Ticket queue");
    expect(screen.queryByRole("heading", { name: "Merchant sign-in (demo)" })).toBeNull();
  });
});
