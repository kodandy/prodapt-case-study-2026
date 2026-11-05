import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createHarness,
  idempotencyKeysOf,
  jsonResponse,
  networkError,
  pendingResponse,
  type Harness,
  type Responder,
} from "../../../app/__tests__/harness";
import { PayMerchantPanel } from "../PayMerchantPanel";

const PAY_PATH = "/api/gw/v1/me/payments";
const MERCHANT_ID = "3f2b8c1e-5a47-4d0e-9c3b-0a1f2e3d4c5b";

const SUCCEEDED_BODY = {
  payment_id: "6d0f1c52-8b0e-4a31-a1d7-2f6f3c9b7e10",
  state: "succeeded",
  amount_minor: 1250,
  currency: "USD",
  method: "card",
  card: { bin: "424242", last4: "4242" },
  decline_code: null,
};

const DECLINED_BODY = {
  ...SUCCEEDED_BODY,
  state: "declined",
  card: { bin: "400000", last4: "0002" },
  decline_code: "card_declined",
};

const EXPECTED_BODY = {
  merchant_id: MERCHANT_ID,
  amount_minor: 1250,
  currency: "USD",
  card: { number: "4242424242424242", exp_month: 12, exp_year: 2030, cvc: "123" },
};

function setup(responder: Responder) {
  const harness = createHarness(responder);
  harness.store.setUserSession("user-token");
  const onPaid = vi.fn<() => void>();
  harness.renderUi(<PayMerchantPanel onPaid={onPaid} />);
  return { harness, onPaid };
}

function isDisabled(element: HTMLElement): boolean {
  return element.hasAttribute("disabled");
}

function fillForm(amount = "12.50"): void {
  fireEvent.change(screen.getByLabelText("Merchant ID"), { target: { value: MERCHANT_ID } });
  fireEvent.change(screen.getByLabelText("Amount"), { target: { value: amount } });
  fireEvent.change(screen.getByLabelText("Card number"), { target: { value: "4242424242424242" } });
  fireEvent.change(screen.getByLabelText("Expiry month"), { target: { value: "12" } });
  fireEvent.change(screen.getByLabelText("Expiry year"), { target: { value: "2030" } });
  fireEvent.change(screen.getByLabelText("CVC"), { target: { value: "123" } });
}

function review(amount = "12.50"): void {
  fillForm(amount);
  fireEvent.click(screen.getByRole("button", { name: "Review" }));
}

function confirm(): void {
  fireEvent.click(screen.getByRole("button", { name: "Confirm payment" }));
}

/** Waits until the Confirm button is gone or usable again, i.e. the request finished. */
async function untilRequestFinished(): Promise<void> {
  await waitFor(() => {
    const button = screen.queryByRole("button", { name: "Confirm payment" });
    expect(button === null || !isDisabled(button)).toBe(true);
  });
}

function feedbackText(): string {
  return [...screen.queryAllByRole("alert"), ...screen.queryAllByRole("status")]
    .map((element) => element.textContent ?? "")
    .join(" ");
}

function postKeys(harness: Harness): string[] {
  return idempotencyKeysOf(harness.sent("POST", PAY_PATH));
}

const UNKNOWN_OUTCOMES: { name: string; respond: Responder }[] = [
  { name: "a network error", respond: () => networkError() },
  { name: "a 503 response", respond: () => jsonResponse(503, { detail: "unavailable" }) },
  { name: "an idempotency_in_progress response", respond: () => jsonResponse(409, { error: "idempotency_in_progress" }) },
];

describe("PayMerchantPanel", () => {
  afterEach(() => {
    cleanup();
  });

  describe("structure", () => {
    it("labels the section by its Pay a merchant heading", () => {
      // Arrange
      setup(() => jsonResponse(201, SUCCEEDED_BODY));

      // Act
      const region = screen.getByRole("region", { name: "Pay a merchant" });

      // Assert
      expect(region.querySelector("h2")?.textContent).toBe("Pay a merchant");
    });

    it("offers a labelled currency select with the supported currencies", () => {
      // Arrange
      setup(() => jsonResponse(201, SUCCEEDED_BODY));

      // Act
      const select = screen.getByLabelText("Currency");

      // Assert
      expect(select.tagName).toBe("SELECT");
      expect(Array.from(select.querySelectorAll("option")).map((o) => o.value)).toEqual(["USD", "EUR", "JPY", "INR"]);
    });

    it("sends nothing until the payment is confirmed", () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(201, SUCCEEDED_BODY));

      // Act
      review();

      // Assert
      expect(screen.getByRole("button", { name: "Confirm payment" }).textContent).toBe("Confirm payment");
      expect(screen.getByRole("button", { name: "Cancel" }).textContent).toBe("Cancel");
      expect(harness.sent("POST", PAY_PATH)).toHaveLength(0);
    });

    it("returns to the form without a request when Cancel is pressed", () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(201, SUCCEEDED_BODY));
      review();

      // Act
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

      // Assert
      expect(screen.queryByRole("button", { name: "Confirm payment" })).toBeNull();
      expect(screen.getByRole("button", { name: "Review" }).textContent).toBe("Review");
      expect(harness.requests).toHaveLength(0);
    });
  });

  describe("validation", () => {
    it.each(["", "abc", "0", "-5", "12.505"])("shows a message and sends nothing for the amount %j", (amount) => {
      // Arrange
      const { harness } = setup(() => jsonResponse(201, SUCCEEDED_BODY));

      // Act
      review(amount);

      // Assert
      expect(feedbackText()).not.toBe("");
      expect(screen.queryByRole("button", { name: "Confirm payment" })).toBeNull();
      expect(harness.requests).toHaveLength(0);
    });
  });

  describe("request", () => {
    it("posts the payment body with the user session and an Idempotency-Key", async () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(201, SUCCEEDED_BODY));
      review();

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      const posts = harness.sent("POST", PAY_PATH);
      expect(posts).toHaveLength(1);
      expect(posts[0]?.body).toEqual(EXPECTED_BODY);
      expect(posts[0]?.headers.get("Authorization")).toBe("Bearer user-token");
      expect(postKeys(harness)[0]).not.toBe("");
    });

    it("converts the amount in the chosen currency to minor units", async () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(201, { ...SUCCEEDED_BODY, currency: "JPY", amount_minor: 1500 }));
      fireEvent.change(screen.getByLabelText("Currency"), { target: { value: "JPY" } });
      review("1500");

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      const body = harness.sent("POST", PAY_PATH)[0]?.body;
      expect(body).toEqual({ ...EXPECTED_BODY, amount_minor: 1500, currency: "JPY" });
    });
  });

  describe("when the payment succeeds", () => {
    it("shows a Paid status and returns to the form", async () => {
      // Arrange
      setup(() => jsonResponse(201, SUCCEEDED_BODY));
      review();

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      await waitFor(() => expect(feedbackText()).toContain("Paid"));
      expect(screen.queryByRole("button", { name: "Confirm payment" })).toBeNull();
      expect(screen.getByRole("button", { name: "Review" }).textContent).toBe("Review");
    });

    it("calls onPaid exactly once", async () => {
      // Arrange
      const { harness, onPaid } = setup(() => jsonResponse(201, SUCCEEDED_BODY));
      review();

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      await waitFor(() => expect(onPaid).toHaveBeenCalledTimes(1));
      expect(harness.sent("POST", PAY_PATH)).toHaveLength(1);
    });
  });

  describe("when the payment is declined", () => {
    it("says the payment was declined and does not call onPaid", async () => {
      // Arrange
      const { onPaid } = setup(() => jsonResponse(201, DECLINED_BODY));
      review();

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      await waitFor(() => expect(feedbackText()).toMatch(/declined/i));
      expect(feedbackText()).not.toContain("Paid");
      expect(onPaid).toHaveBeenCalledTimes(0);
    });
  });

  describe("when the merchant does not exist", () => {
    it("shows that no merchant has that ID and keeps onPaid silent", async () => {
      // Arrange
      const { onPaid } = setup(() => jsonResponse(404, { error: "merchant_not_found" }));
      review();

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      await waitFor(() => expect(feedbackText()).toContain("No merchant with that ID."));
      expect(screen.queryByRole("button", { name: "Confirm payment" })).toBeNull();
      expect(onPaid).toHaveBeenCalledTimes(0);
    });

    it("uses a different Idempotency-Key for the next review and confirm", async () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(404, { error: "merchant_not_found" }));
      review();
      confirm();
      await untilRequestFinished();
      await waitFor(() => expect(screen.queryByRole("button", { name: "Confirm payment" })).toBeNull());
      const firstKeys = new Set(postKeys(harness));

      // Act
      review();
      confirm();
      await untilRequestFinished();

      // Assert
      const secondKeys = new Set(postKeys(harness).filter((key) => !firstKeys.has(key)));
      expect(firstKeys.size).toBe(1);
      expect([...firstKeys][0]).not.toBe("");
      expect(secondKeys.size).toBe(1);
    });
  });

  describe("when the outcome of the payment is unknown", () => {
    it.each(UNKNOWN_OUTCOMES)("keeps the confirm step after $name", async ({ respond }) => {
      // Arrange
      const { harness, onPaid } = setup(respond);
      review();

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      expect(screen.getByRole("button", { name: "Confirm payment" }).textContent).toBe("Confirm payment");
      expect(harness.sent("POST", PAY_PATH).length).toBeGreaterThan(0);
      expect(onPaid).toHaveBeenCalledTimes(0);
    });

    it.each(UNKNOWN_OUTCOMES)("retries with the same Idempotency-Key and body after $name", async ({ respond }) => {
      // Arrange
      const { harness } = setup(respond);
      review();
      confirm();
      await untilRequestFinished();
      const postsAfterFirstConfirm = harness.sent("POST", PAY_PATH).length;

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      const keys = postKeys(harness);
      expect(keys.length).toBeGreaterThan(postsAfterFirstConfirm);
      expect(keys[0]).not.toBe("");
      expect(new Set(keys).size).toBe(1);
      for (const post of harness.sent("POST", PAY_PATH)) {
        expect(post.body).toEqual(EXPECTED_BODY);
      }
    });

    it("reuses the key when the same payment is reviewed again after Cancel", async () => {
      // Arrange
      const { harness } = setup(() => jsonResponse(503, { detail: "unavailable" }));
      review();
      confirm();
      await untilRequestFinished();
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

      // Act
      review();
      confirm();
      await untilRequestFinished();

      // Assert
      const keys = postKeys(harness);
      expect(keys[0]).not.toBe("");
      expect(new Set(keys).size).toBe(1);
    });

    it("lets the retry succeed and then calls onPaid once", async () => {
      // Arrange
      let serverRecovered = false;
      const { onPaid } = setup(() =>
        serverRecovered ? jsonResponse(201, SUCCEEDED_BODY) : jsonResponse(503, { detail: "unavailable" }),
      );
      review();
      confirm();
      await untilRequestFinished();
      serverRecovered = true;

      // Act
      confirm();
      await untilRequestFinished();

      // Assert
      await waitFor(() => expect(onPaid).toHaveBeenCalledTimes(1));
      expect(screen.queryByRole("button", { name: "Confirm payment" })).toBeNull();
    });
  });

  describe("while the payment request is pending", () => {
    it("disables Confirm and Cancel and sends one request for a double click", async () => {
      // Arrange
      const pending = pendingResponse();
      const { harness } = setup(() => pending.promise);
      review();

      // Act
      confirm();
      confirm();

      // Assert
      await waitFor(() => expect(harness.sent("POST", PAY_PATH)).toHaveLength(1));
      expect(isDisabled(screen.getByRole("button", { name: "Confirm payment" }))).toBe(true);
      expect(isDisabled(screen.getByRole("button", { name: "Cancel" }))).toBe(true);

      await act(async () => {
        pending.resolve(jsonResponse(201, SUCCEEDED_BODY));
      });
    });
  });
});
