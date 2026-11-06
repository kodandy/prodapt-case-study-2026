import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse } from "../../../app/__tests__/harness";
import { OpsConsole } from "../OpsConsole";
import { createOpsHarness, type OpsRequest } from "./opsHarness";

const MERCHANT_A = "6f1c2b9e-1a2b-4c3d-8e9f-0a1b2c3d4e5f";
const MERCHANT_B = "0b7d3e1f-9a8b-4c7d-8e6f-5a4b3c2d1e0f";
const SESSION_TOKEN = "ops-session-token-XYZ";
const OPS_AI_TOKEN = "ops-ai-token-XYZ";

function alert(id: string, merchantId: string, metric: string) {
  return {
    alert_id: id,
    merchant_id: merchantId,
    metric,
    detail: null,
    bucket_start: "2026-11-05T09:00:00Z",
    observed: 0.4,
    expected: 0.05,
    zscore: 6.2,
    attempts: 120,
    created_at: "2026-11-05T09:05:00Z",
    updated_at: "2026-11-05T09:05:00Z",
  };
}

const FORECAST = {
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

const RISK = {
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
  items: [
    {
      payment_id: "pay_risky_1",
      amount_minor: 5000,
      currency: "USD",
      occurred_at: "2026-11-05T08:00:00Z",
      probability: 0.42,
      contributions: { log_amount_usd: 0.3 },
    },
  ],
};

interface Script {
  login?: Response;
  opsAiTokens?: () => Response;
  alerts?: (request: OpsRequest) => Response;
  forecast?: (request: OpsRequest) => Response;
  risk?: (request: OpsRequest) => Response;
}

function responder(script: Script = {}) {
  return (request: OpsRequest): Response => {
    if (request.path === "/api/gw/v1/ops/sessions") {
      return (
        script.login ??
        jsonResponse(201, { access_token: SESSION_TOKEN, token_type: "bearer", expires_in: 900, role: "ops_agent" })
      );
    }
    if (request.path === "/api/gw/v1/ops/ops-ai-tokens") {
      return script.opsAiTokens?.() ?? jsonResponse(201, { access_token: OPS_AI_TOKEN, expires_in: 900 });
    }
    if (request.path === "/api/ops/v1/ops/analytics/alerts") {
      return script.alerts?.(request) ?? jsonResponse(200, { server_time: "2026-11-05T10:00:00Z", items: [] });
    }
    if (request.path === "/api/ops/v1/ops/analytics/forecast") return script.forecast?.(request) ?? jsonResponse(200, FORECAST);
    if (request.path === "/api/ops/v1/ops/analytics/chargeback-risk") return script.risk?.(request) ?? jsonResponse(200, RISK);
    return jsonResponse(404, { detail: "not found" });
  };
}

function fillSignIn(values: { email?: string; password?: string; code?: string }): void {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: values.email ?? "agent@ops.example" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: values.password ?? "pw-secret" } });
  fireEvent.change(screen.getByLabelText("Authenticator code"), { target: { value: values.code ?? "123456" } });
}

function submitSignIn(): void {
  fireEvent.click(screen.getByRole("button", { name: "Sign in as ops staff" }));
}

async function signIn(harness: ReturnType<typeof createOpsHarness>): Promise<void> {
  harness.renderUi(<OpsConsole />);
  fillSignIn({});
  submitSignIn();
  await screen.findByRole("heading", { name: "Cross-merchant anomaly alerts" });
}

describe("OpsConsole as the ops principal", () => {
  afterEach(() => {
    cleanup();
  });

  describe("sign-in form", () => {
    it("offers the ops staff sign-in with a TOTP field next to the merchant sign-in, without calling the server", () => {
      // Arrange
      const harness = createOpsHarness(responder());

      // Act
      harness.renderUi(<OpsConsole />);

      // Assert
      expect(screen.getByRole("heading", { name: "Ops staff sign-in" }).textContent).toBe("Ops staff sign-in");
      expect(screen.getByLabelText("Password").getAttribute("type")).toBe("password");
      expect(screen.getByLabelText("Authenticator code").getAttribute("autocomplete")).toBe("one-time-code");
      expect(screen.getByRole("heading", { name: "Merchant sign-in (demo)" }).textContent).toBe("Merchant sign-in (demo)");
      expect(harness.requests).toHaveLength(0);
    });

    it("requires the 6-digit TOTP code before calling the server", async () => {
      // Arrange
      const harness = createOpsHarness(responder());
      harness.renderUi(<OpsConsole />);
      fillSignIn({ code: "" });

      // Act
      submitSignIn();

      // Assert
      expect((await screen.findByRole("alert")).textContent).toBe("Enter the 6-digit code from your authenticator app.");
      expect(harness.requests).toHaveLength(0);
    });

    it("signs in with email, password and TOTP code, then gets the ops-ai token with the ops session", async () => {
      // Arrange
      const harness = createOpsHarness(responder());

      // Act
      await signIn(harness);

      // Assert
      const [login] = harness.sent("POST", "/api/gw/v1/ops/sessions");
      expect(login?.body).toEqual({ email: "agent@ops.example", password: "pw-secret", totp_code: "123456" });
      expect(login?.authorization).toBeNull();
      const [issue] = harness.sent("POST", "/api/gw/v1/ops/ops-ai-tokens");
      expect(issue?.authorization).toBe(`Bearer ${SESSION_TOKEN}`);
      expect(harness.opsStore.get()).toEqual({ sessionToken: SESSION_TOKEN, role: "ops_agent", opsAiToken: OPS_AI_TOKEN });
      expect(screen.getByText("Signed in as ops staff (role ops_agent)").textContent).toBe(
        "Signed in as ops staff (role ops_agent)",
      );
      expect(screen.queryByRole("heading", { name: "Ops staff sign-in" })).toBeNull();
    });

    it("shows the credentials error, clears the password and code, and stays signed out", async () => {
      // Arrange
      const harness = createOpsHarness(responder({ login: jsonResponse(401, { detail: "invalid credentials" }) }));
      harness.renderUi(<OpsConsole />);
      fillSignIn({});

      // Act
      submitSignIn();

      // Assert
      expect((await screen.findByRole("alert")).textContent).toBe("Email or password is incorrect.");
      expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("");
      expect((screen.getByLabelText("Authenticator code") as HTMLInputElement).value).toBe("");
      expect((screen.getByLabelText("Email") as HTMLInputElement).value).toBe("agent@ops.example");
      expect(harness.sent("POST", "/api/gw/v1/ops/ops-ai-tokens")).toHaveLength(0);
      expect(harness.opsStore.get().sessionToken).toBeNull();
    });

    it("shows the lockout message on 429", async () => {
      // Arrange
      const harness = createOpsHarness(responder({ login: jsonResponse(429, { detail: "too many attempts" }) }));
      harness.renderUi(<OpsConsole />);
      fillSignIn({});

      // Act
      submitSignIn();

      // Assert
      expect((await screen.findByRole("alert")).textContent).toBe("Too many failed attempts. Try again later.");
      expect(harness.opsStore.get().sessionToken).toBeNull();
    });

    it("stays signed out when the ops-ai token cannot be issued", async () => {
      // Arrange
      const harness = createOpsHarness(responder({ opsAiTokens: () => jsonResponse(403, { detail: "forbidden" }) }));
      harness.renderUi(<OpsConsole />);
      fillSignIn({});

      // Act
      submitSignIn();

      // Assert
      expect((await screen.findByRole("alert")).textContent).toBe("Your role cannot open this view.");
      expect(harness.opsStore.get()).toEqual({ sessionToken: null, role: null, opsAiToken: null });
      expect(screen.getByRole("heading", { name: "Ops staff sign-in" }).textContent).toBe("Ops staff sign-in");
    });
  });

  describe("signed in", () => {
    it("loads cross-merchant alerts from the ops route with the ops-ai token and never calls the merchant routes", async () => {
      // Arrange
      const harness = createOpsHarness(
        responder({
          alerts: () =>
            jsonResponse(200, {
              server_time: "2026-11-05T10:00:00Z",
              items: [alert("a-1", MERCHANT_A, "failure_rate"), alert("a-2", MERCHANT_B, "volume")],
            }),
        }),
      );

      // Act
      await signIn(harness);
      const items = await screen.findAllByTestId("alert-item");

      // Assert
      const [alertsCall] = harness.sent("GET", "/api/ops/v1/ops/analytics/alerts");
      expect(alertsCall?.authorization).toBe(`Bearer ${OPS_AI_TOKEN}`);
      expect(alertsCall?.query.get("limit")).toBe("50");
      expect(items.map((item) => item.getAttribute("data-merchant-id"))).toEqual([MERCHANT_A, MERCHANT_B]);
      expect(harness.requests.filter((r) => r.path.startsWith("/api/ops/v1/analytics/"))).toHaveLength(0);
      expect(harness.requests.filter((r) => r.path === "/api/gw/v1/merchant-tokens")).toHaveLength(0);
    });

    it("loads forecast and chargeback risk for a typed merchant id from the ops routes", async () => {
      // Arrange
      const harness = createOpsHarness(responder());
      await signIn(harness);

      // Act
      fireEvent.change(screen.getByLabelText("Merchant ID"), { target: { value: ` ${MERCHANT_A} ` } });
      fireEvent.click(screen.getByRole("button", { name: "Load analytics" }));
      const row = await screen.findByTestId("risk-row");

      // Assert
      const [forecast] = harness.sent("GET", "/api/ops/v1/ops/analytics/forecast");
      const [risk] = harness.sent("GET", "/api/ops/v1/ops/analytics/chargeback-risk");
      expect(forecast?.query.get("merchant_id")).toBe(MERCHANT_A);
      expect(forecast?.authorization).toBe(`Bearer ${OPS_AI_TOKEN}`);
      expect(risk?.query.get("merchant_id")).toBe(MERCHANT_A);
      expect(risk?.authorization).toBe(`Bearer ${OPS_AI_TOKEN}`);
      expect(row.getAttribute("data-payment-id")).toBe("pay_risky_1");
      expect(screen.getByRole("table", { name: "USD forecast" }).textContent).toContain("2026-11-06");
      expect(screen.getByText(`Merchant ${MERCHANT_A}`).textContent).toBe(`Merchant ${MERCHANT_A}`);
    });

    it("refuses a merchant id that is not a UUID without calling the server", async () => {
      // Arrange
      const harness = createOpsHarness(responder());
      await signIn(harness);

      // Act
      fireEvent.change(screen.getByLabelText("Merchant ID"), { target: { value: "mch_0001" } });
      fireEvent.click(screen.getByRole("button", { name: "Load analytics" }));

      // Assert
      expect(screen.getByRole("alert").textContent).toBe("Enter the merchant's ID (a UUID).");
      expect(harness.sent("GET", "/api/ops/v1/ops/analytics/forecast")).toHaveLength(0);
      expect(harness.sent("GET", "/api/ops/v1/ops/analytics/chargeback-risk")).toHaveLength(0);
    });

    it("opens a merchant's analytics from one of its alerts", async () => {
      // Arrange
      const harness = createOpsHarness(
        responder({
          alerts: () =>
            jsonResponse(200, { server_time: "2026-11-05T10:00:00Z", items: [alert("a-2", MERCHANT_B, "volume")] }),
        }),
      );
      await signIn(harness);
      await screen.findAllByTestId("alert-item");

      // Act
      fireEvent.click(screen.getByRole("button", { name: `Analyse merchant ${MERCHANT_B}` }));
      await screen.findByTestId("risk-row");

      // Assert
      expect(harness.sent("GET", "/api/ops/v1/ops/analytics/forecast")[0]?.query.get("merchant_id")).toBe(MERCHANT_B);
      expect(harness.sent("GET", "/api/ops/v1/ops/analytics/chargeback-risk")[0]?.query.get("merchant_id")).toBe(MERCHANT_B);
      expect((screen.getByLabelText("Merchant ID") as HTMLInputElement).value).toBe(MERCHANT_B);
    });

    it("shows a role error when an ops route answers 403, and stays signed in", async () => {
      // Arrange
      const harness = createOpsHarness(responder({ alerts: () => jsonResponse(403, { detail: "forbidden" }) }));

      // Act
      await signIn(harness);

      // Assert
      expect((await screen.findByRole("alert")).textContent).toBe("Your role cannot open this view.");
      expect(harness.opsStore.get().sessionToken).toBe(SESSION_TOKEN);
    });

    it("returns to the sign-in form when the ops session has expired", async () => {
      // Arrange
      let issued = 0;
      const harness = createOpsHarness(
        responder({
          opsAiTokens: () => {
            issued += 1;
            return issued === 1
              ? jsonResponse(201, { access_token: OPS_AI_TOKEN, expires_in: 900 })
              : jsonResponse(401, { detail: "invalid ops session" });
          },
          alerts: () => jsonResponse(401, { detail: "invalid token" }),
        }),
      );

      // Act
      harness.renderUi(<OpsConsole />);
      fillSignIn({});
      submitSignIn();

      // Assert
      expect((await screen.findByRole("heading", { name: "Ops staff sign-in" })).textContent).toBe("Ops staff sign-in");
      await waitFor(() => {
        expect(harness.opsStore.get()).toEqual({ sessionToken: null, role: null, opsAiToken: null });
      });
      expect(harness.sent("POST", "/api/gw/v1/ops/ops-ai-tokens")).toHaveLength(2);
    });

    it("signs out of the ops session only, keeping a merchant sign-in", async () => {
      // Arrange
      const harness = createOpsHarness(responder());
      await signIn(harness);
      act(() => {
        harness.store.setMerchant("merchant-key", "merchant-ops-token");
      });

      // Act
      fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

      // Assert
      expect(harness.opsStore.get()).toEqual({ sessionToken: null, role: null, opsAiToken: null });
      expect(harness.store.get().merchantApiKey).toBe("merchant-key");
    });

    it("never renders the session or ops-ai token into the page", async () => {
      // Arrange
      const harness = createOpsHarness(responder());

      // Act
      await signIn(harness);

      // Assert
      const html = document.body.innerHTML;
      expect(html).not.toContain(SESSION_TOKEN);
      expect(html).not.toContain(OPS_AI_TOKEN);
      expect(html).not.toContain("pw-secret");
    });
  });
});
