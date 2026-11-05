import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { createHarness, jsonResponse, pendingResponse } from "../../../app/__tests__/harness";
import { nth } from "../../../app/__tests__/urlHarness";
import { FailureQueryPanel } from "../FailureQueryPanel";

const INVESTIGATE_PATH = "/api/ops/v1/assistant/investigate";

const ANSWER = {
  intent: "failure_investigation",
  cause_label: "bin_spike",
  mode: "llm",
  text: "Declines concentrate on BIN 411111 since 09:00.",
  model: "test-model",
  window: {
    start: "2026-11-05T04:00:00Z",
    end: "2026-11-05T10:00:00Z",
    total: 1200,
    failed: 100,
    rate: 0.0831,
    by_bin: [],
    by_code: [],
  },
  baseline: null,
  suggested_fixes: ["Contact the issuer for BIN 411111", "Enable 3DS for the affected BIN"],
  tool_calls: [{ name: "aggregate_failures", args: { start: "2026-11-05T04:00:00Z" } }],
  evidence: [
    { ref: "aggregate:window", summary: "100 of 1200 payments failed in the last 6 hours" },
    { ref: "bin:411111", summary: "82 percent of the window failures share this BIN" },
  ],
};

function signedIn(responder: Parameters<typeof createHarness>[0]) {
  const harness = createHarness(responder);
  harness.store.setMerchant("merchant-key", "ops-token");
  return harness;
}

function ask(question: string): void {
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: "Investigate" }));
}

describe("FailureQueryPanel", () => {
  afterEach(() => {
    cleanup();
  });

  it("posts the trimmed question as the only body field with the merchant ops token", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, ANSWER));
    harness.renderUi(<FailureQueryPanel />);

    // Act
    ask("  Why did payments fail this morning?  ");
    await screen.findByText(/bin_spike/);

    // Assert
    const requests = harness.sent("POST", INVESTIGATE_PATH);
    expect(requests).toHaveLength(1);
    expect(nth(requests, 0).body).toEqual({ question: "Why did payments fail this morning?" });
    expect(nth(requests, 0).headers.get("Authorization")).toBe("Bearer ops-token");
  });

  it("sends nothing for an empty or whitespace-only question", () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, ANSWER));
    harness.renderUi(<FailureQueryPanel />);

    // Act
    ask("   ");

    // Assert
    expect(harness.requests).toHaveLength(0);
  });

  it("shows the heading, the question field and the Investigate button", () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, ANSWER));

    // Act
    harness.renderUi(<FailureQueryPanel />);

    // Assert
    expect(screen.getByRole("heading", { name: "Failure query" }).textContent).toBe("Failure query");
    expect(screen.getByLabelText("Question").tagName).toBe("TEXTAREA");
    expect(screen.getByRole("button", { name: "Investigate" }).textContent).toBe("Investigate");
  });

  it("renders the cause label, the answer text and the window failure rate as a percent", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, ANSWER));
    harness.renderUi(<FailureQueryPanel />);

    // Act
    ask("What happened?");

    // Assert
    await screen.findByText(/bin_spike/);
    expect(screen.getByText("Declines concentrate on BIN 411111 since 09:00.").textContent).toBe(
      "Declines concentrate on BIN 411111 since 09:00.",
    );
    expect(screen.getAllByText(/8\.3%/)).toHaveLength(1);
  });

  it("lists the suggested fixes", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, ANSWER));
    harness.renderUi(<FailureQueryPanel />);

    // Act
    ask("What happened?");

    // Assert
    await screen.findByText(/bin_spike/);
    expect(screen.getByText(/Suggested fixes/).textContent).toContain("Suggested fixes");
    expect(screen.getByText("Contact the issuer for BIN 411111").tagName).toBe("LI");
    expect(screen.getByText("Enable 3DS for the affected BIN").tagName).toBe("LI");
  });

  it("lists every evidence reference with its summary", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, ANSWER));
    harness.renderUi(<FailureQueryPanel />);

    // Act
    ask("What happened?");

    // Assert
    await screen.findByText(/bin_spike/);
    expect(screen.getAllByText(/aggregate:window/)).toHaveLength(1);
    expect(screen.getAllByText(/100 of 1200 payments failed in the last 6 hours/)).toHaveLength(1);
    expect(screen.getAllByText(/bin:411111/)).toHaveLength(1);
    expect(screen.getAllByText(/82 percent of the window failures share this BIN/)).toHaveLength(1);
  });

  it("lists the tool calls by name", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, ANSWER));
    harness.renderUi(<FailureQueryPanel />);

    // Act
    ask("What happened?");

    // Assert
    await screen.findByText(/bin_spike/);
    expect(screen.getAllByText(/aggregate_failures/)).toHaveLength(1);
  });

  it("omits the window rate when the answer has no window", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, { ...ANSWER, window: null }));
    harness.renderUi(<FailureQueryPanel />);

    // Act
    ask("What happened?");

    // Assert
    await screen.findByText(/bin_spike/);
    expect(screen.queryAllByText(/8\.3%/)).toHaveLength(0);
  });

  it("says the LLM is unavailable and shows only evidence in the t2_fallback mode", async () => {
    // Arrange
    const fallback = { ...ANSWER, mode: "t2_fallback", text: null, model: null };
    const harness = signedIn(() => jsonResponse(200, fallback));
    harness.renderUi(<FailureQueryPanel />);

    // Act
    ask("What happened?");

    // Assert
    const notice = await screen.findByText("LLM unavailable: evidence only");
    expect(notice.textContent).toBe("LLM unavailable: evidence only");
    expect(screen.queryAllByText("Declines concentrate on BIN 411111 since 09:00.")).toHaveLength(0);
    expect(screen.getAllByText(/aggregate:window/)).toHaveLength(1);
  });

  it("does not show the fallback notice for an LLM answer", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(200, ANSWER));
    harness.renderUi(<FailureQueryPanel />);

    // Act
    ask("What happened?");

    // Assert
    await screen.findByText(/bin_spike/);
    expect(screen.queryAllByText("LLM unavailable: evidence only")).toHaveLength(0);
  });

  it("disables the button while the request is pending and ignores a second click", async () => {
    // Arrange
    const pending = pendingResponse();
    const harness = signedIn(() => pending.promise);
    harness.renderUi(<FailureQueryPanel />);
    const button = screen.getByRole("button", { name: "Investigate" });
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "What happened?" } });

    // Act
    fireEvent.click(button);
    fireEvent.click(button);

    // Assert
    expect(button.hasAttribute("disabled")).toBe(true);
    expect(harness.sent("POST", INVESTIGATE_PATH)).toHaveLength(1);
    pending.resolve(jsonResponse(200, ANSWER));
    await waitFor(() => expect(button.hasAttribute("disabled")).toBe(false));
  });

  it("shows the mapped message in an alert when the request fails", async () => {
    // Arrange
    const harness = signedIn(() => jsonResponse(503, { detail: "try again later" }));
    harness.renderUi(<FailureQueryPanel />);

    // Act
    ask("What happened?");

    // Assert
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("The service is busy. Try again later.");
    expect(screen.queryAllByText(/bin_spike/)).toHaveLength(0);
  });
});
