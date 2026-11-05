import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse, type Responder } from "../../../app/__tests__/harness";
import { createQueryHarness } from "../../../app/__tests__/queryHarness";
import { DisputesPanel } from "../DisputesPanel";

const LIST_PATH = "/api/gw/v1/disputes";
const PAYMENT_ID = "6d0f1c52-8b0e-4a31-a1d7-2f6f3c9b7e10";
const EVIDENCE_TEXT = "Signed delivery receipt, tracking number 1Z999AA10123456784.";

interface DisputeFixture {
  dispute_id: string;
  payment_id: string;
  state: string;
  amount_minor: number;
  currency: string;
  reason: string;
  chargeback_fee_minor: number;
  created_at: string;
  updated_at: string;
}

function dispute(id: string, state: string): DisputeFixture {
  return {
    dispute_id: id,
    payment_id: PAYMENT_ID,
    state,
    amount_minor: 2500,
    currency: "USD",
    reason: "fraudulent",
    chargeback_fee_minor: 0,
    created_at: "2026-10-01T12:00:00Z",
    updated_at: "2026-10-01T12:00:00Z",
  };
}

const OPENED = dispute("d-open", "opened");
const SECOND_OPENED = dispute("d-open-2", "opened");
const UNDER_REVIEW = dispute("d-review", "under_review");
const WON = dispute("d-won", "won");

function serving(items: DisputeFixture[], other: Responder = () => jsonResponse(404, { detail: "not found" })): Responder {
  return (request) =>
    request.method === "GET" && request.path === LIST_PATH
      ? jsonResponse(200, { items, next_cursor: null })
      : other(request);
}

function setup(responder: Responder) {
  const harness = createQueryHarness(responder);
  harness.store.setMerchant("merchant-api-key", "ops-token");
  harness.renderUi(<DisputesPanel />);
  return harness;
}

function evidenceFields(): HTMLElement[] {
  return screen.queryAllByLabelText("Evidence");
}

function submitButtons(): HTMLElement[] {
  return screen.queryAllByRole("button", { name: /Submit evidence/ });
}

function stateCount(state: string): number {
  return screen.queryAllByText(state).length;
}

function feedbackText(): string {
  return [...screen.queryAllByRole("alert"), ...screen.queryAllByRole("status")]
    .map((element) => element.textContent ?? "")
    .join(" ");
}

function type(field: HTMLElement | undefined, text: string): void {
  if (field === undefined) throw new Error("expected field to exist");
  fireEvent.change(field, { target: { value: text } });
}

function click(element: HTMLElement | undefined): void {
  if (element === undefined) throw new Error("expected element to exist");
  fireEvent.click(element);
}

describe("DisputesPanel", () => {
  afterEach(() => {
    cleanup();
  });

  describe("structure and initial load", () => {
    it("labels the section by its Disputes heading", async () => {
      // Arrange
      setup(serving([OPENED]));

      // Act
      const region = screen.getByRole("region", { name: "Disputes" });
      await waitFor(() => expect(evidenceFields()).toHaveLength(1));

      // Assert
      expect(region.querySelector("h2")?.textContent).toBe("Disputes");
    });

    it("loads the first page with limit 20 using the merchant API key, not the user session", async () => {
      // Arrange
      const harness = createQueryHarness(serving([OPENED]));
      harness.store.setUserSession("user-token");
      harness.store.setMerchant("merchant-api-key", "ops-token");
      harness.renderUi(<DisputesPanel />);

      // Act
      await waitFor(() => expect(evidenceFields()).toHaveLength(1));

      // Assert
      const gets = harness.sent("GET", LIST_PATH);
      expect(gets).toHaveLength(1);
      expect(gets[0]?.headers.get("Authorization")).toBe("Bearer merchant-api-key");
      expect(harness.queriesOf("GET", LIST_PATH)[0]?.get("limit")).toBe("20");
    });

    it("shows the state of every dispute", async () => {
      // Arrange
      setup(serving([OPENED, UNDER_REVIEW, WON]));

      // Act
      await waitFor(() => expect(evidenceFields()).toHaveLength(1));

      // Assert
      expect(stateCount("opened")).toBe(1);
      expect(stateCount("under_review")).toBe(1);
      expect(stateCount("won")).toBe(1);
    });

    it("shows a role alert when the list cannot be loaded", async () => {
      // Arrange
      setup(() => jsonResponse(500, { detail: "boom" }));

      // Act
      const alert = await screen.findByRole("alert");

      // Assert
      expect(alert.textContent).not.toBe("");
      expect(evidenceFields()).toHaveLength(0);
    });
  });

  describe("evidence form", () => {
    it("offers the Evidence field and Submit evidence only on opened disputes", async () => {
      // Arrange
      setup(serving([OPENED, UNDER_REVIEW, WON]));

      // Act
      await waitFor(() => expect(evidenceFields()).toHaveLength(1));

      // Assert
      expect(evidenceFields()[0]?.tagName).toBe("TEXTAREA");
      expect(submitButtons()).toHaveLength(1);
    });

    it("posts the text to the evidence path of that dispute with the merchant key", async () => {
      // Arrange
      const harness = setup(
        serving([OPENED, SECOND_OPENED], () => jsonResponse(200, dispute("d-open-2", "under_review"))),
      );
      await waitFor(() => expect(evidenceFields()).toHaveLength(2));
      type(evidenceFields()[1], EVIDENCE_TEXT);

      // Act
      click(submitButtons()[1]);

      // Assert
      await waitFor(() => expect(stateCount("under_review")).toBe(1));
      const posts = harness.sent("POST", `${LIST_PATH}/d-open-2/evidence`);
      expect(posts).toHaveLength(1);
      expect(posts[0]?.body).toEqual({ text: EVIDENCE_TEXT });
      expect(posts[0]?.headers.get("Authorization")).toBe("Bearer merchant-api-key");
      expect(harness.sent("POST", `${LIST_PATH}/d-open/evidence`)).toHaveLength(0);
    });

    it("moves the dispute to under_review and removes its evidence form after a 200", async () => {
      // Arrange
      setup(serving([OPENED, UNDER_REVIEW], () => jsonResponse(200, dispute("d-open", "under_review"))));
      await waitFor(() => expect(evidenceFields()).toHaveLength(1));
      type(evidenceFields()[0], EVIDENCE_TEXT);

      // Act
      click(submitButtons()[0]);

      // Assert
      await waitFor(() => expect(stateCount("under_review")).toBe(2));
      expect(stateCount("opened")).toBe(0);
      expect(evidenceFields()).toHaveLength(0);
      expect(submitButtons()).toHaveLength(0);
    });

    it.each(["", "   ", "\n\t"])("shows a message and sends nothing for blank evidence %j", async (blank) => {
      // Arrange
      const harness = setup(serving([OPENED]));
      await waitFor(() => expect(evidenceFields()).toHaveLength(1));
      type(evidenceFields()[0], blank);

      // Act
      click(submitButtons()[0]);

      // Assert
      await waitFor(() => expect(feedbackText()).not.toBe(""));
      expect(harness.requests.filter((r) => r.method === "POST")).toHaveLength(0);
      expect(stateCount("opened")).toBe(1);
    });

    it("shows an error and keeps the dispute opened after a 409", async () => {
      // Arrange
      setup(serving([OPENED], () => jsonResponse(409, { detail: "invalid dispute transition" })));
      await waitFor(() => expect(evidenceFields()).toHaveLength(1));
      type(evidenceFields()[0], EVIDENCE_TEXT);

      // Act
      click(submitButtons()[0]);

      // Assert
      await waitFor(() => expect(feedbackText()).not.toBe(""));
      expect(stateCount("opened")).toBe(1);
      expect(stateCount("under_review")).toBe(0);
    });

    it("shows an error when the dispute is not found", async () => {
      // Arrange
      setup(serving([OPENED], () => jsonResponse(404, { detail: "dispute not found" })));
      await waitFor(() => expect(evidenceFields()).toHaveLength(1));
      type(evidenceFields()[0], EVIDENCE_TEXT);

      // Act
      click(submitButtons()[0]);

      // Assert
      await waitFor(() => expect(feedbackText()).not.toBe(""));
      expect(stateCount("under_review")).toBe(0);
    });
  });
});
