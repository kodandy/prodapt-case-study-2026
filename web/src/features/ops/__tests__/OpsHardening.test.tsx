import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiResult } from "../../../lib/api";
import {
  createOpsSessionStore,
  type OpsApiClient,
  type OpsApiRequest,
  type OpsSignInOutcome,
} from "../../../lib/opsSession";
import { OpsAlertsPanel } from "../OpsAlertsPanel";
import { OpsMerchantAnalytics } from "../OpsMerchantAnalytics";
import { OpsServicesProvider } from "../opsServices";
import { OpsSignIn } from "../OpsSignIn";

const NETWORK_MESSAGE = "Could not reach the server. Check your connection and try again.";
const UUID_MESSAGE = "Enter the merchant's ID (a UUID).";
const MERCHANT = "6f1c2b9e-1a2b-4c3d-8e9f-0a1b2c3d4e5f";

type RequestImpl = (req: OpsApiRequest) => Promise<ApiResult<unknown>>;
type SignInImpl = () => Promise<OpsSignInOutcome>;

/** A client stub that can throw, which the real client never does on purpose; the UI must survive it anyway. */
function stubClient(request: RequestImpl, signIn: SignInImpl = () => Promise.resolve({ ok: true })): OpsApiClient {
  return {
    signIn: () => signIn(),
    request: <T,>(req: OpsApiRequest) => request(req) as Promise<ApiResult<T>>,
  };
}

function renderWith(client: OpsApiClient, ui: ReactNode): void {
  render(<OpsServicesProvider services={{ store: createOpsSessionStore(), client }}>{ui}</OpsServicesProvider>);
}

function inputValue(label: string): string {
  const element = screen.getByLabelText(label);
  if (!(element instanceof HTMLInputElement)) throw new Error(`${label} is not an input`);
  return element.value;
}

function buttonDisabled(name: string): boolean {
  const element = screen.getByRole("button", { name });
  if (!(element instanceof HTMLButtonElement)) throw new Error(`${name} is not a button`);
  return element.disabled;
}

const forecastBody = {
  generated_at: "2026-11-05T10:00:00Z",
  horizon_days: 7,
  history_days: 56,
  currencies: [
    {
      currency: "USD",
      alpha: 0.3,
      history: [{ day: "2026-11-04", amount_minor: 100000 }],
      forecast: [{ day: "2026-11-06", expected_minor: 123400, low_minor: 100000, high_minor: 150000 }],
      backtest: { horizon_days: 7, folds: 3, mae_minor: 1000, wape: 0.12, naive_wape: 0.2 },
    },
  ],
};

const riskBody = {
  captured_count: 10,
  chargeback_count: 1,
  merchant_chargeback_rate: 0.1,
  model: {
    features: ["log_amount_usd"],
    train_count: 100,
    test_count: 20,
    test_positives: 2,
    auc: 0.81,
    brier: 0.05,
    base_rate_brier: 0.09,
    trained_on: "synthetic",
  },
  items: [],
};

afterEach(() => {
  cleanup();
});

describe("OpsSignIn when sign-in throws", () => {
  it("shows an error, clears the password and code, and lets the user try again", async () => {
    // Arrange
    const signIn = vi
      .fn<SignInImpl>()
      .mockRejectedValueOnce(new TypeError("body stream failed"))
      .mockResolvedValueOnce({ ok: false, code: "invalid credentials" });
    renderWith(stubClient(() => Promise.resolve({ ok: true, status: 200, data: null }), signIn), <OpsSignIn />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "agent@ops.example" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw-secret" } });
    fireEvent.change(screen.getByLabelText("Authenticator code"), { target: { value: "123456" } });

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Sign in as ops staff" }));

    // Assert
    expect((await screen.findByRole("alert")).textContent).toBe(NETWORK_MESSAGE);
    expect(inputValue("Password")).toBe("");
    expect(inputValue("Authenticator code")).toBe("");
    expect(buttonDisabled("Sign in as ops staff")).toBe(false);
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw-secret" } });
    fireEvent.change(screen.getByLabelText("Authenticator code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in as ops staff" }));
    await waitFor(() => expect(signIn).toHaveBeenCalledTimes(2));
  });
});

describe("OpsAlertsPanel when a poll throws", () => {
  it("shows an error and keeps polling", async () => {
    // Arrange
    const request = vi
      .fn<RequestImpl>()
      .mockRejectedValueOnce(new TypeError("body stream failed"))
      .mockResolvedValue({ ok: true, status: 200, data: { server_time: "2026-11-05T10:00:00Z", items: [] } });

    // Act
    renderWith(stubClient(request), <OpsAlertsPanel pollMs={5} onSelectMerchant={() => undefined} />);

    // Assert
    expect((await screen.findByText("No anomalies detected on any merchant.")).textContent).toBe(
      "No anomalies detected on any merchant.",
    );
    expect(request.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});

describe("OpsMerchantAnalytics hardening", () => {
  it("shows an error and re-enables the button when loading throws", async () => {
    // Arrange
    const request = vi.fn<RequestImpl>().mockRejectedValue(new TypeError("body stream failed"));
    renderWith(stubClient(request), <OpsMerchantAnalytics requested={null} />);
    fireEvent.change(screen.getByLabelText("Merchant ID"), { target: { value: MERCHANT } });

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Load analytics" }));

    // Assert
    expect((await screen.findByRole("alert")).textContent).toBe(NETWORK_MESSAGE);
    expect(buttonDisabled("Load analytics")).toBe(false);
  });

  it("validates a merchant id picked from an alert before calling the server", async () => {
    // Arrange
    const request = vi.fn<RequestImpl>();

    // Act
    renderWith(stubClient(request), <OpsMerchantAnalytics requested={{ merchantId: "../../admin", seq: 1 }} />);

    // Assert
    expect((await screen.findByRole("alert")).textContent).toBe(UUID_MESSAGE);
    expect(request).not.toHaveBeenCalled();
  });

  it("still loads a valid merchant id picked from an alert", async () => {
    // Arrange
    const request = vi.fn<RequestImpl>((req) =>
      Promise.resolve({ ok: true, status: 200, data: req.path.endsWith("/forecast") ? forecastBody : riskBody }),
    );

    // Act
    renderWith(stubClient(request), <OpsMerchantAnalytics requested={{ merchantId: MERCHANT, seq: 1 }} />);

    // Assert
    expect((await screen.findByText(`Merchant ${MERCHANT}`)).textContent).toBe(`Merchant ${MERCHANT}`);
    expect(request.mock.calls.map(([req]) => req.query?.merchant_id)).toEqual([MERCHANT, MERCHANT]);
  });
});
