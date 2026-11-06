import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  createHarness,
  jsonResponse,
  networkError,
  pendingResponse,
  type Harness,
  type Responder,
} from "../../../app/__tests__/harness";
import { AssistantQueryPanel, type ToolQueryAnswer } from "../AssistantQueryPanel";

const QUERY_PATH = "/api/ops/v1/assistant/query";
const TOKEN_PATH = "/api/gw/v1/merchant-tokens";
const QUESTION = "Why did card payments fail in the last 6 hours?";

const RULES_ANSWER: ToolQueryAnswer = {
  abstained: false,
  mode: "llm",
  text: "Failures rose because the issuer was unavailable [reason:issuer_unavailable]",
  model: "mock",
  planner_model: "rules",
  claims: [{ text: "Issuer unavailable is the top reason", evidence_refs: ["reason:issuer_unavailable"] }],
  tool_calls: [
    { name: "compare_baseline", args: { lookback_hours: "6", method: "card" } },
    { name: "failure_reasons", args: { lookback_hours: "6" } },
  ],
  rejected_calls: [{ name: "delete_everything", reason: "unknown_tool" }],
  evidence: [
    { ref: "aggregate:window", summary: "12 of 40 card payments failed" },
    { ref: "reason:issuer_unavailable", summary: "9 failures: issuer_unavailable" },
  ],
};

const LLM_ANSWER: ToolQueryAnswer = {
  ...RULES_ANSWER,
  mode: "t2_fallback",
  text: null,
  model: null,
  planner_model: "gpt-4.1-mini",
  rejected_calls: [],
};

function answering(answer: ToolQueryAnswer): Responder {
  return (request) =>
    request.method === "POST" && request.path === QUERY_PATH
      ? jsonResponse(200, answer)
      : jsonResponse(404, { detail: "not found" });
}

function setup(responder: Responder): Harness {
  const harness = createHarness(responder);
  harness.store.setMerchant("merchant-api-key", "ops-token");
  harness.renderUi(<AssistantQueryPanel />);
  return harness;
}

function ask(question: string): void {
  fireEvent.change(screen.getByLabelText("Free-form prompt"), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: "Run tool query" }));
}

function textsOf(testId: string): string[] {
  return screen.queryAllByTestId(testId).map((element) => element.textContent ?? "");
}

describe("AssistantQueryPanel", () => {
  afterEach(() => {
    cleanup();
  });

  describe("success", () => {
    it("posts only the trimmed question to the query route with the ops token", async () => {
      // Arrange
      const harness = setup(answering(RULES_ANSWER));

      // Act
      ask(`  ${QUESTION}  `);
      await screen.findByTestId("tool-query-answer");

      // Assert
      const sent = harness.sent("POST", QUERY_PATH);
      expect(sent).toHaveLength(1);
      expect(sent[0]?.body).toStrictEqual({ question: QUESTION });
      expect(sent[0]?.headers.get("Authorization")).toBe("Bearer ops-token");
    });

    it("shows the answer text, the evidence refs and the rule-based planner with the mock narration", async () => {
      // Arrange
      setup(answering(RULES_ANSWER));

      // Act
      ask(QUESTION);
      await screen.findByTestId("tool-query-answer");

      // Assert
      expect(screen.getByTestId("tool-query-text").textContent).toBe(RULES_ANSWER.text);
      expect(screen.getByTestId("tool-query-planner").textContent).toBe("rules (rule-based)");
      expect(screen.getByTestId("tool-query-narration").textContent).toBe("llm mock (mock)");
      expect(screen.getByTestId("tool-query-abstained").textContent).toBe("no");
      expect(textsOf("tool-query-evidence-item")).toStrictEqual([
        "aggregate:window — 12 of 40 card payments failed",
        "reason:issuer_unavailable — 9 failures: issuer_unavailable",
      ]);
      expect(screen.getByTestId("tool-query-claims").textContent).toBe(
        "Issuer unavailable is the top reason [reason:issuer_unavailable]",
      );
    });

    it("labels an LLM planner by its model and an evidence-only answer as such", async () => {
      // Arrange
      setup(answering(LLM_ANSWER));

      // Act
      ask(QUESTION);
      await screen.findByTestId("tool-query-answer");

      // Assert
      expect(screen.getByTestId("tool-query-planner").textContent).toBe("gpt-4.1-mini (LLM)");
      expect(screen.getByTestId("tool-query-narration").textContent).toBe("t2_fallback (evidence only)");
      expect(screen.queryByTestId("tool-query-text")).toBeNull();
      expect(screen.queryByTestId("tool-query-rejected")).toBeNull();
    });
  });

  describe("tool-call display", () => {
    it("lists every called tool in order with its arguments exactly as returned", async () => {
      // Arrange
      setup(answering(RULES_ANSWER));

      // Act
      ask(QUESTION);
      await screen.findByTestId("tool-query-answer");

      // Assert
      const calls = screen.getAllByTestId("tool-query-call");
      expect(calls.map((call) => call.querySelector("code")?.textContent)).toStrictEqual([
        "compare_baseline",
        "failure_reasons",
      ]);
      expect(textsOf("tool-query-call-args")).toStrictEqual(["lookback_hours=6, method=card", "lookback_hours=6"]);
    });

    it("lists rejected tool calls with their reason", async () => {
      // Arrange
      setup(answering(RULES_ANSWER));

      // Act
      ask(QUESTION);
      await screen.findByTestId("tool-query-answer");

      // Assert
      expect(screen.getByTestId("tool-query-rejected").textContent).toBe("delete_everything unknown_tool");
    });

    it("says no tools were called when the planner chose none", async () => {
      // Arrange
      setup(answering({ ...RULES_ANSWER, tool_calls: [], abstained: true }));

      // Act
      ask(QUESTION);
      await screen.findByTestId("tool-query-answer");

      // Assert
      expect(screen.getByText("No tools were called.").tagName).toBe("P");
      expect(screen.getByTestId("tool-query-abstained").textContent).toBe("yes");
    });
  });

  describe("pending", () => {
    it("disables submit while the request is pending and sends nothing more on a second click", async () => {
      // Arrange
      const reply = pendingResponse();
      const harness = setup(() => reply.promise);

      // Act
      ask(QUESTION);
      const button = screen.getByRole("button", { name: "Running…" });
      fireEvent.click(button);
      fireEvent.submit(button);

      // Assert
      expect(button).toHaveProperty("disabled", true);
      expect(harness.sent("POST", QUERY_PATH)).toHaveLength(1);
      reply.resolve(jsonResponse(200, RULES_ANSWER));
      await screen.findByTestId("tool-query-answer");
      expect(screen.getByRole("button", { name: "Run tool query" })).toHaveProperty("disabled", false);
    });
  });

  describe("errors and Retry", () => {
    it("shows an alert and Retry on a 5xx, and Retry resends the same question", async () => {
      // Arrange
      let calls = 0;
      const harness = setup((request) => {
        if (request.path !== QUERY_PATH) return jsonResponse(404, { detail: "not found" });
        calls += 1;
        return calls === 1 ? jsonResponse(500, { detail: "boom" }) : jsonResponse(200, RULES_ANSWER);
      });
      ask(QUESTION);
      const alert = await screen.findByRole("alert");

      // Act
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await screen.findByTestId("tool-query-answer");

      // Assert
      expect(alert.textContent).toBe("Something went wrong. Please try again.");
      expect(harness.sent("POST", QUERY_PATH).map((request) => request.body)).toStrictEqual([
        { question: QUESTION },
        { question: QUESTION },
      ]);
      expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.queryByRole("status")).toBeNull();
    });

    it("shows an alert and Retry on a 422 without staying in Loading", async () => {
      // Arrange
      setup(() => jsonResponse(422, { error: "validation_failed" }));

      // Act
      ask(QUESTION);
      const alert = await screen.findByRole("alert");

      // Assert
      expect(alert.textContent).toBe("Some fields are not valid.");
      expect(screen.getByRole("button", { name: "Retry" })).toHaveProperty("disabled", false);
      expect(screen.queryByRole("status")).toBeNull();
      expect(screen.getByRole("button", { name: "Run tool query" })).toHaveProperty("disabled", false);
    });

    it("shows the network message and Retry when fetch fails", async () => {
      // Arrange
      setup(() => networkError());

      // Act
      ask(QUESTION);
      const alert = await screen.findByRole("alert");

      // Assert
      expect(alert.textContent).toBe("Could not reach the server. Check your connection and try again.");
      expect(screen.getByRole("button", { name: "Retry" })).toHaveProperty("disabled", false);
      expect(screen.queryByTestId("tool-query-answer")).toBeNull();
    });

    it("drops the previous answer when a later question fails", async () => {
      // Arrange
      let calls = 0;
      setup(() => {
        calls += 1;
        return calls === 1 ? jsonResponse(200, RULES_ANSWER) : jsonResponse(503, { detail: "try again later" });
      });
      ask(QUESTION);
      await screen.findByTestId("tool-query-answer");

      // Act
      ask("and yesterday?");
      await screen.findByRole("alert");

      // Assert
      expect(screen.queryByTestId("tool-query-answer")).toBeNull();
    });
  });

  describe("stale responses", () => {
    it("ignores an answer that arrives after the merchant changed", async () => {
      // Arrange
      const reply = pendingResponse();
      const harness = setup(() => reply.promise);
      ask(QUESTION);

      // Act
      act(() => {
        harness.store.setMerchant("other-merchant-key", "other-ops-token");
      });
      await act(async () => {
        reply.resolve(jsonResponse(200, RULES_ANSWER));
        await reply.promise;
      });

      // Assert
      expect(screen.queryByTestId("tool-query-answer")).toBeNull();
      expect(screen.queryByRole("status")).toBeNull();
      expect(screen.getByRole("button", { name: "Run tool query" })).toHaveProperty("disabled", false);
    });

    it("ignores an error that arrives after the merchant changed", async () => {
      // Arrange
      const reply = pendingResponse();
      const harness = setup(() => reply.promise);
      ask(QUESTION);

      // Act
      act(() => {
        harness.store.setMerchant("other-merchant-key", "other-ops-token");
      });
      await act(async () => {
        reply.resolve(jsonResponse(500, { detail: "boom" }));
        await reply.promise;
      });

      // Assert
      expect(screen.queryByRole("alert")).toBeNull();
    });

    it("keeps the answer when only the ops token was refreshed after a 401", async () => {
      // Arrange
      let queries = 0;
      const harness = setup((request) => {
        if (request.path === TOKEN_PATH) return jsonResponse(200, { access_token: "fresh-ops-token" });
        queries += 1;
        return queries === 1 ? jsonResponse(401, { detail: "invalid token" }) : jsonResponse(200, RULES_ANSWER);
      });

      // Act
      ask(QUESTION);
      await screen.findByTestId("tool-query-answer");

      // Assert
      await waitFor(() => expect(harness.sent("POST", QUERY_PATH)).toHaveLength(2));
      expect(harness.sent("POST", QUERY_PATH)[1]?.headers.get("Authorization")).toBe("Bearer fresh-ops-token");
    });
  });
});
