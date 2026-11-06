import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { jsonResponse } from "../app/__tests__/harness";
import { App } from "../App";

function respond(input: RequestInfo | URL): Response {
  const path = String(input).split("?")[0];
  if (path === "/api/gw/v1/ops/sessions") {
    return jsonResponse(201, { access_token: "app-ops-session", token_type: "bearer", expires_in: 900, role: "ops_lead" });
  }
  if (path === "/api/gw/v1/ops/ops-ai-tokens") return jsonResponse(201, { access_token: "app-ops-ai", expires_in: 900 });
  if (path === "/api/ops/v1/ops/analytics/alerts") return jsonResponse(200, { server_time: "2026-11-05T10:00:00Z", items: [] });
  return jsonResponse(404, { detail: "not found" });
}

describe("App ops sign-in", () => {
  let fetchMock: Mock<typeof fetch>;

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>((input) => Promise.resolve(respond(input)));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("signs ops staff in from the Ops console tab and shows the cross-merchant view", async () => {
    // Arrange
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Ops console" }));
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "lead@ops.example" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.change(screen.getByLabelText("Authenticator code"), { target: { value: "000000" } });

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Sign in as ops staff" }));

    // Assert
    expect((await screen.findByText("No anomalies detected on any merchant.")).textContent).toBe(
      "No anomalies detected on any merchant.",
    );
    const alertsCall = fetchMock.mock.calls.find(([input]) => String(input).startsWith("/api/ops/v1/ops/analytics/alerts"));
    expect(new Headers(alertsCall?.[1]?.headers).get("Authorization")).toBe("Bearer app-ops-ai");
  });

  it("keeps the ops session out of the Dashboard and Merchant areas, which still ask for the merchant key", async () => {
    // Arrange
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Ops console" }));
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "lead@ops.example" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.change(screen.getByLabelText("Authenticator code"), { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in as ops staff" }));
    await screen.findByText("No anomalies detected on any merchant.");

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Dashboard" }));

    // Assert
    expect(screen.getByRole("heading", { name: "Merchant sign-in (demo)" }).textContent).toBe("Merchant sign-in (demo)");
  });
});
