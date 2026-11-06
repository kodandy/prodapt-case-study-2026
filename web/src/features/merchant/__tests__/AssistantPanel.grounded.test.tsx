import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse, type Responder } from "../../../app/__tests__/harness";
import { createQueryHarness } from "../../../app/__tests__/queryHarness";
import { AssistantPanel } from "../AssistantPanel";

const ASK_PATH = "/api/ops/v1/assistant/ask";
const AGGREGATE_REF = "aggregate:decline_code:card_declined";
const SENTENCE = `- 2 declined payments with decline code card_declined [${AGGREGATE_REF}]`;
const PLAIN_BODY = { mode: "llm", text: "plain narration", model: "mock", evidence: [] };
const GROUNDED_BODY = {
  mode: "llm",
  text: SENTENCE,
  model: "mock",
  evidence: [{ ref: AGGREGATE_REF, summary: "2 declined payments with decline code card_declined" }],
  grounded: true,
  citations: [
    { sentence: SENTENCE, sources: [{ ref: AGGREGATE_REF, excerpt: "declined payments with decline code card_declined" }] },
  ],
  unsupported_sentences: [
    { sentence: "Another merchant lost 9000 USD [payment:not-retrieved].", reason: "unknown_ref", refs: ["payment:not-retrieved"] },
  ],
  relevant_refs: [AGGREGATE_REF],
};

function setup(responder: Responder) {
  const harness = createQueryHarness(responder);
  harness.store.setMerchant("merchant-api-key", "ops-token");
  harness.renderUi(<AssistantPanel knownPaymentIds={new Set()} onOpenPayment={() => undefined} />);
  fireEvent.change(screen.getByRole("combobox", { name: /Mode/ }), { target: { value: "/v1/assistant/ask" } });
  fireEvent.change(screen.getByRole("textbox", { name: /Question/ }), { target: { value: "declined payments" } });
  return harness;
}

describe("AssistantPanel grounded /ask (R1-5)", () => {
  afterEach(cleanup);

  it("sends grounded=true when the box is checked and shows each sentence with its source excerpt", async () => {
    // Arrange
    const harness = setup((request) =>
      request.path === ASK_PATH ? jsonResponse(200, GROUNDED_BODY) : jsonResponse(404, { detail: "not found" }),
    );
    fireEvent.click(screen.getByRole("checkbox", { name: /Grounded citations/ }));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    const cited = await screen.findByTestId("cited-sentences");

    // Assert
    expect(harness.queriesOf("POST", ASK_PATH).map((q) => q.get("grounded"))).toEqual(["true"]);
    expect(within(cited).getByText(AGGREGATE_REF).closest("li")?.textContent).toBe(
      `${AGGREGATE_REF}: declined payments with decline code card_declined`,
    );
    expect(screen.getByTestId("grounded-citations").querySelector("p")?.textContent).toBe("Grounded in the sources below.");
    expect(screen.getByTestId("unsupported-sentences").textContent).toBe(
      "Cites a source not retrieved (payment:not-retrieved): Another merchant lost 9000 USD [payment:not-retrieved].",
    );
  });

  it("sends no grounded parameter and shows no citations when the box is left unchecked", async () => {
    // Arrange
    const harness = setup((request) =>
      request.path === ASK_PATH ? jsonResponse(200, PLAIN_BODY) : jsonResponse(404, { detail: "not found" }),
    );

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("plain narration");

    // Assert
    expect(harness.queriesOf("POST", ASK_PATH).map((q) => q.get("grounded"))).toEqual([null]);
    expect(screen.queryByTestId("grounded-citations")).toBeNull();
  });

  it("does not offer the grounded box outside the Ask mode", () => {
    // Arrange
    setup(() => jsonResponse(404, { detail: "not found" }));

    // Act
    fireEvent.change(screen.getByRole("combobox", { name: /Mode/ }), {
      target: { value: "/v1/assistant/investigate" },
    });

    // Assert
    expect(screen.queryByRole("checkbox", { name: /Grounded citations/ })).toBeNull();
  });
});
