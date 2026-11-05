import { act, cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse } from "../../../app/__tests__/harness";
import { createUrlHarness, type UrlRequest } from "../../../app/__tests__/urlHarness";
import { DashboardArea } from "../DashboardArea";

function respond(request: UrlRequest): Response {
  if (request.path === "/api/ops/v1/analytics/forecast") {
    return jsonResponse(200, { generated_at: "2026-11-05T00:00:00Z", horizon_days: 7, history_days: 28, currencies: [] });
  }
  if (request.path === "/api/ops/v1/analytics/chargeback-risk") {
    return jsonResponse(200, {
      captured_count: 0,
      chargeback_count: 0,
      merchant_chargeback_rate: 0,
      model: {
        features: [],
        train_count: 0,
        test_count: 0,
        test_positives: 0,
        auc: null,
        brier: 0.006,
        base_rate_brier: 0.006,
        trained_on: "seed 42",
      },
      items: [],
    });
  }
  if (request.path === "/api/ops/v1/analytics/alerts") {
    return jsonResponse(200, { server_time: "2026-11-05T10:00:00Z", items: [] });
  }
  return jsonResponse(404, { detail: "not found" });
}

describe("DashboardArea", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows the merchant sign-in and no panels while signed out", () => {
    // Arrange
    const harness = createUrlHarness(respond);

    // Act
    harness.renderUi(<DashboardArea />);

    // Assert
    expect(screen.getByRole("heading", { name: "Merchant sign-in (demo)" }).textContent).toBe("Merchant sign-in (demo)");
    expect(screen.queryByRole("heading", { name: "Settlement forecast" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Chargeback risk" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Live anomaly alerts" })).toBeNull();
    expect(harness.requests).toHaveLength(0);
  });

  it("shows the three panels once the merchant is signed in", async () => {
    // Arrange
    const harness = createUrlHarness(respond);
    harness.store.setMerchant("merchant-key", "ops-token");

    // Act
    harness.renderUi(<DashboardArea />);

    // Assert
    expect((await screen.findByRole("heading", { name: "Settlement forecast" })).textContent).toBe("Settlement forecast");
    expect(screen.getByRole("heading", { name: "Chargeback risk" }).textContent).toBe("Chargeback risk");
    expect(screen.getByRole("heading", { name: "Live anomaly alerts" }).textContent).toBe("Live anomaly alerts");
    expect(screen.queryByRole("heading", { name: "Merchant sign-in (demo)" })).toBeNull();
  });

  it("loads each analytics endpoint once on sign-in", async () => {
    // Arrange
    const harness = createUrlHarness(respond);
    harness.store.setMerchant("merchant-key", "ops-token");

    // Act
    harness.renderUi(<DashboardArea />);
    await screen.findByText("No anomalies detected.");

    // Assert
    expect(harness.sent("GET", "/api/ops/v1/analytics/forecast")).toHaveLength(1);
    expect(harness.sent("GET", "/api/ops/v1/analytics/chargeback-risk")).toHaveLength(1);
    expect(harness.sent("GET", "/api/ops/v1/analytics/alerts")).toHaveLength(1);
  });

  it("swaps the sign-in for the panels when credentials arrive and back when they are cleared", async () => {
    // Arrange
    const harness = createUrlHarness(respond);
    harness.renderUi(<DashboardArea />);

    // Act
    act(() => {
      harness.store.setMerchant("merchant-key", "ops-token");
    });
    const forecastHeading = await screen.findByRole("heading", { name: "Settlement forecast" });
    act(() => {
      harness.store.clearMerchant();
    });

    // Assert
    expect(forecastHeading.textContent).toBe("Settlement forecast");
    expect(screen.getByRole("heading", { name: "Merchant sign-in (demo)" }).textContent).toBe("Merchant sign-in (demo)");
    expect(screen.queryByRole("heading", { name: "Settlement forecast" })).toBeNull();
  });
});
