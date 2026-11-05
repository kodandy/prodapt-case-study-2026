import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse } from "../../../app/__tests__/harness";
import { createUrlHarness, nth } from "../../../app/__tests__/urlHarness";
import { ChargebackRiskPanel } from "../ChargebackRiskPanel";

const RISK_PATH = "/api/ops/v1/analytics/chargeback-risk";

const MODEL = {
  features: ["log_amount_usd", "cross_border", "prior_decline_24h", "merchant_cb_rate"],
  train_count: 8400,
  test_count: 3600,
  test_positives: 21,
  auc: 0.6123,
  brier: 0.00589,
  base_rate_brier: 0.006,
  trained_on: "seed 42, 12000 records",
};

interface RiskItem {
  payment_id: string;
  amount_minor: number;
  currency: string;
  occurred_at: string;
  probability: number;
  contributions: Record<string, number>;
}

// API order is deliberately not sorted by probability: the panel must not reorder it.
const ITEMS: RiskItem[] = [
  {
    payment_id: "pay-risk-1",
    amount_minor: 25_000,
    currency: "USD",
    occurred_at: "2026-10-30T10:00:00Z",
    probability: 0.0218,
    contributions: { log_amount_usd: 0.9, cross_border: 0.2, prior_decline_24h: 0.1, merchant_cb_rate: 0.05 },
  },
  {
    payment_id: "pay-risk-2",
    amount_minor: 4_500,
    currency: "EUR",
    occurred_at: "2026-10-30T11:00:00Z",
    probability: 0.034,
    contributions: { log_amount_usd: 0.1, cross_border: 0.7, prior_decline_24h: 0.2, merchant_cb_rate: 0.05 },
  },
  {
    payment_id: "pay-risk-3",
    amount_minor: 120_000,
    currency: "JPY",
    occurred_at: "2026-10-30T12:00:00Z",
    probability: 0.0071,
    contributions: { log_amount_usd: 0.1, cross_border: 0.05, prior_decline_24h: 0.4, merchant_cb_rate: 0.02 },
  },
  {
    payment_id: "pay-risk-4",
    amount_minor: 1_000,
    currency: "USD",
    occurred_at: "2026-10-30T13:00:00Z",
    probability: 0.011,
    // Largest absolute value is the negative amount term; the largest value is the chargeback history.
    contributions: { log_amount_usd: -0.9, cross_border: -0.1, prior_decline_24h: 0, merchant_cb_rate: 0.02 },
  },
];

const BODY = {
  captured_count: 500,
  chargeback_count: 6,
  merchant_chargeback_rate: 0.012,
  model: MODEL,
  items: ITEMS,
};

function signedIn(body: unknown, status = 200) {
  const harness = createUrlHarness(() => jsonResponse(status, body));
  harness.store.setMerchant("merchant-key", "ops-token");
  return harness;
}

function riskRows(): HTMLElement[] {
  return screen.getAllByTestId("risk-row");
}

describe("ChargebackRiskPanel", () => {
  afterEach(() => {
    cleanup();
  });

  it("requests the top 20 payments with the merchant ops token", async () => {
    // Arrange
    const harness = signedIn(BODY);

    // Act
    harness.renderUi(<ChargebackRiskPanel />);
    await screen.findAllByTestId("risk-row");

    // Assert
    const requests = harness.sent("GET", RISK_PATH);
    expect(requests).toHaveLength(1);
    expect(nth(requests, 0).query.get("limit")).toBe("20");
    expect(nth(requests, 0).headers.get("Authorization")).toBe("Bearer ops-token");
  });

  it("shows the heading and the merchant chargeback rate with one decimal", async () => {
    // Arrange
    const harness = signedIn(BODY);

    // Act
    harness.renderUi(<ChargebackRiskPanel />);
    const rate = await screen.findByText("Merchant chargeback rate 1.2%");

    // Assert
    expect(rate.textContent).toBe("Merchant chargeback rate 1.2%");
    expect(screen.getByRole("heading", { name: "Chargeback risk" }).textContent).toBe("Chargeback risk");
  });

  it("shows the model AUC to two decimals and Brier scores to four decimals", async () => {
    // Arrange
    const harness = signedIn(BODY);

    // Act
    harness.renderUi(<ChargebackRiskPanel />);

    // Assert
    const line = await screen.findByText("Model AUC 0.61 · Brier 0.0059 (base rate 0.0060)");
    expect(line.textContent).toBe("Model AUC 0.61 · Brier 0.0059 (base rate 0.0060)");
  });

  it("shows n/a when the AUC is null", async () => {
    // Arrange
    const harness = signedIn({ ...BODY, model: { ...MODEL, auc: null } });

    // Act
    harness.renderUi(<ChargebackRiskPanel />);

    // Assert
    const line = await screen.findByText("Model AUC n/a · Brier 0.0059 (base rate 0.0060)");
    expect(line.textContent).toBe("Model AUC n/a · Brier 0.0059 (base rate 0.0060)");
  });

  it("warns that the labels are synthetic", async () => {
    // Arrange
    const harness = signedIn(BODY);

    // Act
    harness.renderUi(<ChargebackRiskPanel />);
    await screen.findAllByTestId("risk-row");

    // Assert
    expect(screen.getAllByText(/synthetic/)).toHaveLength(1);
  });

  it("renders one row per payment in API order", async () => {
    // Arrange
    const harness = signedIn(BODY);

    // Act
    harness.renderUi(<ChargebackRiskPanel />);
    await screen.findAllByTestId("risk-row");

    // Assert
    expect(riskRows().map((row) => row.getAttribute("data-payment-id"))).toEqual([
      "pay-risk-1",
      "pay-risk-2",
      "pay-risk-3",
      "pay-risk-4",
    ]);
  });

  it("shows each probability as a one-decimal percent and the amount in its currency", async () => {
    // Arrange
    const harness = signedIn(BODY);

    // Act
    harness.renderUi(<ChargebackRiskPanel />);
    await screen.findAllByTestId("risk-row");

    // Assert
    const rows = riskRows();
    const first = within(nth(rows, 0));
    expect(first.getByText("2.2%").textContent).toBe("2.2%");
    expect(first.getByText("$250.00").textContent).toBe("$250.00");
    const second = within(nth(rows, 1));
    expect(second.getByText("3.4%").textContent).toBe("3.4%");
    expect(second.getByText("€45.00").textContent).toBe("€45.00");
    const third = within(nth(rows, 2));
    expect(third.getByText("0.7%").textContent).toBe("0.7%");
    expect(third.getByText("¥120,000").textContent).toBe("¥120,000");
  });

  it("labels the feature with the largest contribution for each payment", async () => {
    // Arrange
    const harness = signedIn(BODY);

    // Act
    harness.renderUi(<ChargebackRiskPanel />);
    await screen.findAllByTestId("risk-row");

    // Assert
    const labels = riskRows().map((row) => {
      const cells = within(row).queryAllByText(
        /^(Amount|Cross-border|Recent decline on card|Merchant chargeback history)$/,
      );
      return cells.map((cell) => cell.textContent);
    });
    expect(labels).toEqual([["Amount"], ["Cross-border"], ["Recent decline on card"], ["Merchant chargeback history"]]);
  });

  it("renders no rows for a merchant without scored payments", async () => {
    // Arrange
    const harness = signedIn({ ...BODY, items: [] });

    // Act
    harness.renderUi(<ChargebackRiskPanel />);
    await screen.findByText("Merchant chargeback rate 1.2%");

    // Assert
    expect(screen.queryAllByTestId("risk-row")).toHaveLength(0);
  });

  it("shows the mapped message in an alert when the request fails", async () => {
    // Arrange
    const harness = signedIn({ detail: "try again later" }, 503);

    // Act
    harness.renderUi(<ChargebackRiskPanel />);

    // Assert
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("The service is busy. Try again later.");
    expect(screen.queryAllByTestId("risk-row")).toHaveLength(0);
  });
});
