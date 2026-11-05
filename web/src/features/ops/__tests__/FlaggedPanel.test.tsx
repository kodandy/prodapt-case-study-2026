import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { createHarness, jsonResponse } from "../../../app/__tests__/harness";
import { nth } from "../../../app/__tests__/urlHarness";
import { FlaggedPanel } from "../FlaggedPanel";

const FRAUD_PATH = "/api/ops/v1/assistant/investigate-fraud";
const QUESTION = "Are there signs of card testing on my account?";

interface Episode {
  first_payment_id: string;
  start: string;
  end: string;
  payment_ids: string[];
  micro_payments: number;
  card_payments: number;
  micro_share: number;
  distinct_cards: number | null;
  declined: number;
  decline_ratio: number;
  card_bins: string[];
  currencies: string[];
  flagged: boolean;
  reasons: string[];
}

function episode(overrides: Partial<Episode> & Pick<Episode, "first_payment_id">): Episode {
  return {
    start: "2026-11-05T08:00:00Z",
    end: "2026-11-05T08:10:00Z",
    payment_ids: [],
    micro_payments: 14,
    card_payments: 16,
    micro_share: 0.875,
    distinct_cards: 9,
    declined: 12,
    decline_ratio: 0.857,
    card_bins: ["411111"],
    currencies: ["USD"],
    flagged: true,
    reasons: ["micro_burst", "many_cards"],
    ...overrides,
  };
}

function answer(episodes: Episode[] | null, alertCount = 0): Record<string, unknown> {
  return {
    intent: "card_testing",
    cause_label: episodes?.some((e) => e.flagged) === true ? "card_testing" : "no_card_testing",
    mode: "t2_fallback",
    text: null,
    model: null,
    analysis:
      episodes === null
        ? null
        : { start: "2026-11-04T10:00:00Z", end: "2026-11-05T10:00:00Z", micro_payments: 20, episodes },
    alerts: Array.from({ length: alertCount }, (_, i) => ({
      alert_id: `alert-${i}`,
      ticket_id: null,
      queue: "fraud",
      first_payment_id: `pay-${i}`,
    })),
    suggested_fixes: [],
    tool_calls: [],
    evidence: [],
  };
}

function signedIn(body: Record<string, unknown>, status = 200) {
  const harness = createHarness(() => jsonResponse(status, body));
  harness.store.setMerchant("merchant-key", "ops-token");
  return harness;
}

function scan(): void {
  fireEvent.click(screen.getByRole("button", { name: "Scan for card testing" }));
}

describe("FlaggedPanel", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows the heading and sends nothing before the scan button is pressed", () => {
    // Arrange
    const harness = signedIn(answer([]));

    // Act
    harness.renderUi(<FlaggedPanel />);

    // Assert
    expect(screen.getByRole("heading", { name: "Flagged transactions" }).textContent).toBe("Flagged transactions");
    expect(harness.requests).toHaveLength(0);
  });

  it("posts the fixed card-testing question as the only body field", async () => {
    // Arrange
    const harness = signedIn(answer([episode({ first_payment_id: "ep-1" })]));
    harness.renderUi(<FlaggedPanel />);

    // Act
    scan();
    await screen.findAllByTestId("episode");

    // Assert
    const requests = harness.sent("POST", FRAUD_PATH);
    expect(requests).toHaveLength(1);
    expect(nth(requests, 0).body).toEqual({ question: QUESTION });
    expect(nth(requests, 0).headers.get("Authorization")).toBe("Bearer ops-token");
  });

  it("renders only the flagged episodes with micro payments, decline ratio and reasons", async () => {
    // Arrange
    const flagged = episode({ first_payment_id: "ep-flagged" });
    const quiet = episode({
      first_payment_id: "ep-quiet",
      flagged: false,
      micro_payments: 5,
      decline_ratio: 0.2,
      reasons: [],
    });
    const harness = signedIn(answer([quiet, flagged]));
    harness.renderUi(<FlaggedPanel />);

    // Act
    scan();
    const episodes = await screen.findAllByTestId("episode");

    // Assert
    expect(episodes).toHaveLength(1);
    const text = nth(episodes, 0).textContent ?? "";
    expect(text).toContain("14");
    expect(text).toContain("85.7%");
    expect(text).toContain("micro_burst");
    expect(text).toContain("many_cards");
    expect(document.body.textContent).not.toContain("20.0%");
  });

  it("renders one episode element per flagged episode", async () => {
    // Arrange
    const first = episode({ first_payment_id: "ep-1", micro_payments: 14 });
    const second = episode({ first_payment_id: "ep-2", micro_payments: 31, decline_ratio: 0.5, reasons: ["micro_burst"] });
    const harness = signedIn(answer([first, second]));
    harness.renderUi(<FlaggedPanel />);

    // Act
    scan();
    const episodes = await screen.findAllByTestId("episode");

    // Assert
    expect(episodes).toHaveLength(2);
    expect(nth(episodes, 1).textContent).toContain("31");
    expect(nth(episodes, 0).textContent).toContain("14");
    expect(nth(episodes, 1).textContent).toContain("50.0%");
  });

  it("shows how many alerts were raised", async () => {
    // Arrange
    const harness = signedIn(answer([episode({ first_payment_id: "ep-1" })], 2));
    harness.renderUi(<FlaggedPanel />);

    // Act
    scan();
    await screen.findAllByTestId("episode");

    // Assert
    expect(document.body.textContent).toMatch(/alerts?\W+2\b|\b2\s+alerts?/i);
  });

  it("says there are no flagged episodes when every episode is below the threshold", async () => {
    // Arrange
    const quiet = episode({ first_payment_id: "ep-quiet", flagged: false, reasons: [] });
    const harness = signedIn(answer([quiet]));
    harness.renderUi(<FlaggedPanel />);

    // Act
    scan();

    // Assert
    const empty = await screen.findByText("No flagged episodes.");
    expect(empty.textContent).toBe("No flagged episodes.");
    expect(screen.queryAllByTestId("episode")).toHaveLength(0);
  });

  it("says there are no flagged episodes when the analysis is null", async () => {
    // Arrange
    const harness = signedIn(answer(null));
    harness.renderUi(<FlaggedPanel />);

    // Act
    scan();

    // Assert
    const empty = await screen.findByText("No flagged episodes.");
    expect(empty.textContent).toBe("No flagged episodes.");
    expect(screen.queryAllByTestId("episode")).toHaveLength(0);
  });

  it("shows the mapped message in an alert when the request fails", async () => {
    // Arrange
    const harness = signedIn({ detail: "try again later" }, 503);
    harness.renderUi(<FlaggedPanel />);

    // Act
    scan();

    // Assert
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("The service is busy. Try again later.");
    expect(screen.queryAllByTestId("episode")).toHaveLength(0);
  });
});
