import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse, networkError } from "../../../app/__tests__/harness";
import { createUrlHarness, nth } from "../../../app/__tests__/urlHarness";
import { ForecastPanel } from "../ForecastPanel";

const FORECAST_PATH = "/api/ops/v1/analytics/forecast";

interface ForecastDay {
  day: string;
  expected_minor: number;
  low_minor: number;
  high_minor: number;
}

function usdDays(): ForecastDay[] {
  // expected 10000..16000 in steps of 1000 (sum 91000), low = expected - 2500, high = expected + 3000
  return Array.from({ length: 7 }, (_, i) => ({
    day: `2026-11-0${i + 1}`,
    expected_minor: 10_000 + 1_000 * i,
    low_minor: 7_500 + 1_000 * i,
    high_minor: 13_000 + 1_000 * i,
  }));
}

function jpyDays(): ForecastDay[] {
  return Array.from({ length: 7 }, (_, i) => ({
    day: `2026-11-0${i + 1}`,
    expected_minor: 5_000,
    low_minor: 3_000,
    high_minor: 7_000,
  }));
}

function history(amount: number): { day: string; amount_minor: number }[] {
  return [
    { day: "2026-10-30", amount_minor: amount },
    { day: "2026-10-31", amount_minor: amount + 100 },
  ];
}

const FORECAST_BODY = {
  generated_at: "2026-11-01T00:00:00Z",
  horizon_days: 7,
  history_days: 28,
  currencies: [
    {
      currency: "USD",
      alpha: 0.3,
      history: history(9_000),
      forecast: usdDays(),
      backtest: { horizon_days: 7, folds: 3, mae_minor: 1200.5, wape: 0.123, naive_wape: 0.2 },
    },
    {
      currency: "JPY",
      alpha: 0.5,
      history: history(4_800),
      forecast: jpyDays(),
      backtest: null,
    },
  ],
};

function signedIn(respond: () => Response | Promise<Response>) {
  const harness = createUrlHarness(respond);
  harness.store.setMerchant("merchant-key", "ops-token");
  return harness;
}

describe("ForecastPanel", () => {
  afterEach(() => {
    cleanup();
  });

  it("requests the 7-day forecast once with the merchant ops token", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, FORECAST_BODY));

    // Act
    harness.renderUi(<ForecastPanel />);
    await screen.findByRole("table", { name: "USD forecast" });

    // Assert
    const requests = harness.sent("GET", FORECAST_PATH);
    expect(requests).toHaveLength(1);
    expect(nth(requests, 0).query.get("horizon_days")).toBe("7");
    expect(nth(requests, 0).headers.get("Authorization")).toBe("Bearer ops-token");
  });

  it("shows the section heading", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, FORECAST_BODY));

    // Act
    harness.renderUi(<ForecastPanel />);

    // Assert
    const heading = await screen.findByRole("heading", { name: "Settlement forecast" });
    expect(heading.textContent).toBe("Settlement forecast");
  });

  it("describes each currency chart with the currency, the horizon and the formatted total", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, FORECAST_BODY));

    // Act
    harness.renderUi(<ForecastPanel />);
    await screen.findByRole("table", { name: "USD forecast" });

    // Assert
    const labels = screen.getAllByRole("img").map((img) => img.getAttribute("aria-label") ?? "");
    expect(labels).toHaveLength(2);
    const usd = labels.find((label) => label.includes("USD")) ?? "";
    const jpy = labels.find((label) => label.includes("JPY")) ?? "";
    expect(usd).toContain("next 7 days");
    expect(usd).toContain("$910.00");
    expect(jpy).toContain("next 7 days");
    expect(jpy).toContain("¥35,000");
  });

  it("lists every forecast day with expected, low and high amounts in that order", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, FORECAST_BODY));

    // Act
    harness.renderUi(<ForecastPanel />);
    const usdTable = await screen.findByRole("table", { name: "USD forecast" });

    // Assert
    const rows = within(usdTable).getAllByRole("row");
    expect(rows).toHaveLength(1 + 7);
    const amountsOf = (row: HTMLElement): string[] =>
      within(row)
        .getAllByRole("cell")
        .slice(-3)
        .map((cell) => cell.textContent);
    expect(amountsOf(nth(rows, 1))).toEqual(["$100.00", "$75.00", "$130.00"]);
    expect(amountsOf(nth(rows, 7))).toEqual(["$160.00", "$135.00", "$190.00"]);
  });

  it("formats zero-decimal currencies without a fraction", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, FORECAST_BODY));

    // Act
    harness.renderUi(<ForecastPanel />);
    const jpyTable = await screen.findByRole("table", { name: "JPY forecast" });

    // Assert
    const rows = within(jpyTable).getAllByRole("row");
    expect(rows).toHaveLength(1 + 7);
    const cells = within(nth(rows, 1))
      .getAllByRole("cell")
      .slice(-3)
      .map((cell) => cell.textContent);
    expect(cells).toEqual(["¥5,000", "¥3,000", "¥7,000"]);
  });

  it("shows the backtest WAPE next to the naive baseline with one decimal", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, FORECAST_BODY));

    // Act
    harness.renderUi(<ForecastPanel />);
    await screen.findByRole("table", { name: "USD forecast" });

    // Assert
    expect(screen.getAllByText("Backtest WAPE 12.3% vs naive 20.0%")).toHaveLength(1);
  });

  it("says there is no backtest for a currency whose backtest is null", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, FORECAST_BODY));

    // Act
    harness.renderUi(<ForecastPanel />);
    await screen.findByRole("table", { name: "JPY forecast" });

    // Assert
    expect(screen.getAllByText("No backtest (not enough history)")).toHaveLength(1);
  });

  it("explains an empty forecast when no currency has captured payments", async () => {
    // Arrange
    const harness = signedIn(() =>
      jsonResponse(200, { generated_at: "2026-11-01T00:00:00Z", horizon_days: 7, history_days: 28, currencies: [] }),
    );

    // Act
    harness.renderUi(<ForecastPanel />);

    // Assert
    const empty = await screen.findByText("No captured payments in the last 28 days.");
    expect(empty.textContent).toBe("No captured payments in the last 28 days.");
    expect(screen.queryAllByRole("img")).toHaveLength(0);
    expect(screen.queryAllByRole("table")).toHaveLength(0);
  });

  it("shows the mapped message in an alert when the service is busy", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(503, { detail: "try again later" }));

    // Act
    harness.renderUi(<ForecastPanel />);

    // Assert
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("The service is busy. Try again later.");
    expect(screen.queryAllByRole("table")).toHaveLength(0);
  });

  it("shows the network message in an alert when the request fails", async () => {
    // Arrange
    const harness = signedIn(() => networkError());

    // Act
    harness.renderUi(<ForecastPanel />);

    // Assert
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Could not reach the server. Check your connection and try again.");
  });
});
