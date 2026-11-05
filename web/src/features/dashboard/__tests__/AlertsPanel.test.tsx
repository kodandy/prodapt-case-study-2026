import { act, cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../../../app/__tests__/harness";
import { createUrlHarness, nth, type UrlRequest } from "../../../app/__tests__/urlHarness";
import { AlertsPanel, MAX_ALERTS_SHOWN } from "../AlertsPanel";

const ALERTS_PATH = "/api/ops/v1/analytics/alerts";
const POLL_MS = 30;

interface AlertFixture {
  alert_id: string;
  metric: "failure_rate" | "volume" | "decline_mix";
  detail: string | null;
  bucket_start: string;
  observed: number;
  expected: number;
  zscore: number;
  attempts: number;
  created_at: string;
  updated_at: string;
}

function alertFixture(overrides: Partial<AlertFixture> & Pick<AlertFixture, "alert_id">): AlertFixture {
  return {
    metric: "failure_rate",
    detail: null,
    bucket_start: "2026-11-05T09:45:00Z",
    observed: 0.31,
    expected: 0.08,
    zscore: 4.23,
    attempts: 40,
    created_at: "2026-11-05T09:50:00Z",
    updated_at: "2026-11-05T09:50:00Z",
    ...overrides,
  };
}

function page(serverTime: string, items: AlertFixture[]): Response {
  return jsonResponse(200, { server_time: serverTime, items });
}

function renderedIds(): (string | null)[] {
  return screen.queryAllByTestId("alert-item").map((item) => item.getAttribute("data-alert-id"));
}

function itemById(alertId: string): HTMLElement {
  const item = screen.getAllByTestId("alert-item").find((el) => el.getAttribute("data-alert-id") === alertId);
  if (item === undefined) throw new Error(`no alert item ${alertId}`);
  return item;
}

function signedIn(responder: (request: UrlRequest) => Response) {
  const harness = createUrlHarness(responder);
  harness.store.setMerchant("merchant-key", "ops-token");
  return harness;
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

describe("AlertsPanel", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  describe("initial load", () => {
    it("requests the latest 50 alerts without since, using the merchant ops token", async () => {
      // Arrange
      const harness = signedIn(() => page("2026-11-05T10:00:00Z", [alertFixture({ alert_id: "a1" })]));

      // Act
      harness.renderUi(<AlertsPanel pollMs={60_000} />);
      await screen.findAllByTestId("alert-item");

      // Assert
      const requests = harness.sent("GET", ALERTS_PATH);
      expect(requests).toHaveLength(1);
      expect(nth(requests, 0).query.get("limit")).toBe("50");
      expect(nth(requests, 0).query.has("since")).toBe(false);
      expect(nth(requests, 0).headers.get("Authorization")).toBe("Bearer ops-token");
    });

    it("shows the heading and puts the alert list in a polite live region", async () => {
      // Arrange
      const harness = signedIn(() => page("2026-11-05T10:00:00Z", [alertFixture({ alert_id: "a1" })]));

      // Act
      harness.renderUi(<AlertsPanel pollMs={60_000} />);
      const items = await screen.findAllByTestId("alert-item");

      // Assert
      expect(screen.getByRole("heading", { name: "Live anomaly alerts" }).textContent).toBe("Live anomaly alerts");
      expect(nth(items, 0).closest("[aria-live]")?.getAttribute("aria-live")).toBe("polite");
    });

    it("shows a failure-rate alert with percents, the label and the z-score", async () => {
      // Arrange
      const harness = signedIn(() => page("2026-11-05T10:00:00Z", [alertFixture({ alert_id: "a1" })]));

      // Act
      harness.renderUi(<AlertsPanel pollMs={60_000} />);
      await screen.findAllByTestId("alert-item");

      // Assert
      const text = itemById("a1").textContent ?? "";
      expect(text).toContain("Failure rate");
      expect(text).toContain("31.0%");
      expect(text).toContain("8.0%");
      expect(text).toContain("z=4.2");
    });

    it("shows a volume alert with integer counts and no percent sign", async () => {
      // Arrange
      const volume = alertFixture({ alert_id: "v1", metric: "volume", observed: 87, expected: 24.2, zscore: 5.04 });
      const harness = signedIn(() => page("2026-11-05T10:00:00Z", [volume]));

      // Act
      harness.renderUi(<AlertsPanel pollMs={60_000} />);
      await screen.findAllByTestId("alert-item");

      // Assert
      const text = itemById("v1").textContent ?? "";
      expect(text).toContain("Volume");
      expect(text).toContain("87");
      expect(text).toContain("24");
      expect(text).not.toContain("24.2");
      expect(text).not.toContain("%");
      expect(text).toContain("z=5.0");
    });

    it("shows a decline-mix alert with its detail and percents", async () => {
      // Arrange
      const mix = alertFixture({
        alert_id: "m1",
        metric: "decline_mix",
        detail: "do_not_honor",
        observed: 0.552,
        expected: 0.125,
        zscore: 3.77,
      });
      const harness = signedIn(() => page("2026-11-05T10:00:00Z", [mix]));

      // Act
      harness.renderUi(<AlertsPanel pollMs={60_000} />);
      await screen.findAllByTestId("alert-item");

      // Assert
      const text = itemById("m1").textContent ?? "";
      expect(text).toContain("Decline mix");
      expect(text).toContain("do_not_honor");
      expect(text).toContain("55.2%");
      expect(text).toContain("12.5%");
      expect(text).toContain("z=3.8");
    });

    it("orders alerts newest update first regardless of the response order", async () => {
      // Arrange
      const older = alertFixture({ alert_id: "old", updated_at: "2026-11-05T09:00:00Z" });
      const newest = alertFixture({ alert_id: "new", updated_at: "2026-11-05T09:55:00Z" });
      const middle = alertFixture({ alert_id: "mid", updated_at: "2026-11-05T09:30:00Z" });
      const harness = signedIn(() => page("2026-11-05T10:00:00Z", [older, newest, middle]));

      // Act
      harness.renderUi(<AlertsPanel pollMs={60_000} />);
      await screen.findAllByTestId("alert-item");

      // Assert
      expect(renderedIds()).toEqual(["new", "mid", "old"]);
    });

    it("says no anomalies were detected when there are none", async () => {
      // Arrange
      const harness = signedIn(() => page("2026-11-05T10:00:00Z", []));

      // Act
      harness.renderUi(<AlertsPanel pollMs={60_000} />);

      // Assert
      const empty = await screen.findByText("No anomalies detected.");
      expect(empty.textContent).toBe("No anomalies detected.");
      expect(renderedIds()).toEqual([]);
    });

    it("shows the mapped message in an alert when the first load fails", async () => {
      // Arrange
      const harness = signedIn(() => jsonResponse(503, { detail: "try again later" }));

      // Act
      harness.renderUi(<AlertsPanel pollMs={60_000} />);

      // Assert
      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toBe("The service is busy. Try again later.");
      expect(renderedIds()).toEqual([]);
    });
  });

  describe("polling", () => {
    it("asks again with since set to the previous server_time", async () => {
      // Arrange
      const serverTimes = ["2026-11-05T10:00:00Z", "2026-11-05T10:00:15Z", "2026-11-05T10:00:30Z"];
      let call = 0;
      const harness = signedIn(() => {
        const serverTime = serverTimes[Math.min(call, serverTimes.length - 1)] ?? "";
        call += 1;
        return page(serverTime, []);
      });

      // Act
      harness.renderUi(<AlertsPanel pollMs={POLL_MS} />);
      await waitFor(() => expect(harness.sent("GET", ALERTS_PATH).length).toBeGreaterThanOrEqual(3));

      // Assert
      const requests = harness.sent("GET", ALERTS_PATH);
      expect(nth(requests, 0).query.has("since")).toBe(false);
      expect(nth(requests, 1).query.get("since")).toBe("2026-11-05T10:00:00Z");
      expect(nth(requests, 2).query.get("since")).toBe("2026-11-05T10:00:15Z");
    });

    it("merges new alerts and replaces an existing alert_id with the updated item", async () => {
      // Arrange
      const first = alertFixture({ alert_id: "a1", observed: 0.31, updated_at: "2026-11-05T09:00:00Z" });
      const second = alertFixture({ alert_id: "a2", updated_at: "2026-11-05T09:05:00Z" });
      const updatedFirst = alertFixture({ alert_id: "a1", observed: 0.47, updated_at: "2026-11-05T09:20:00Z" });
      const third = alertFixture({ alert_id: "a3", updated_at: "2026-11-05T09:10:00Z" });
      let call = 0;
      const harness = signedIn(() => {
        call += 1;
        if (call === 1) return page("2026-11-05T09:30:00Z", [second, first]);
        return page("2026-11-05T09:30:15Z", [third, updatedFirst]);
      });

      // Act
      harness.renderUi(<AlertsPanel pollMs={POLL_MS} />);
      await waitFor(() => expect(renderedIds()).toEqual(["a1", "a3", "a2"]));

      // Assert
      expect(screen.getAllByTestId("alert-item")).toHaveLength(3);
      const replaced = itemById("a1").textContent ?? "";
      expect(replaced).toContain("47.0%");
      expect(replaced).not.toContain("31.0%");
    });

    it("keeps showing earlier alerts when a poll returns nothing new", async () => {
      // Arrange
      let call = 0;
      const harness = signedIn(() => {
        call += 1;
        return call === 1
          ? page("2026-11-05T10:00:00Z", [alertFixture({ alert_id: "a1" })])
          : page("2026-11-05T10:00:15Z", []);
      });

      // Act
      harness.renderUi(<AlertsPanel pollMs={POLL_MS} />);
      await waitFor(() => expect(harness.sent("GET", ALERTS_PATH).length).toBeGreaterThanOrEqual(2));
      await sleep(POLL_MS * 2);

      // Assert
      expect(renderedIds()).toEqual(["a1"]);
    });

    it("stops polling after unmount", async () => {
      // Arrange
      const harness = signedIn(() => page("2026-11-05T10:00:00Z", []));
      harness.renderUi(<AlertsPanel pollMs={POLL_MS} />);
      await waitFor(() => expect(harness.sent("GET", ALERTS_PATH).length).toBeGreaterThanOrEqual(2));

      // Act
      cleanup();
      const countAtUnmount = harness.sent("GET", ALERTS_PATH).length;
      await sleep(POLL_MS * 6);

      // Assert
      expect(harness.sent("GET", ALERTS_PATH)).toHaveLength(countAtUnmount);
    });
  });

  describe("re-delivered alerts and list cap", () => {
    const BASE_UPDATE_MS = Date.parse("2026-11-05T00:00:00Z");

    function numberedAlert(index: number): AlertFixture {
      const updatedAt = new Date(BASE_UPDATE_MS + index * 60_000).toISOString().replace(".000Z", "Z");
      return alertFixture({ alert_id: `a${String(index).padStart(3, "0")}`, updated_at: updatedAt });
    }

    function numberedAlerts(from: number, to: number): AlertFixture[] {
      return Array.from({ length: to - from }, (_, offset) => numberedAlert(from + offset));
    }

    function newestFirstIds(from: number, to: number): string[] {
      return numberedAlerts(from, to)
        .map((alert) => alert.alert_id)
        .reverse();
    }

    it("exports the shown-alert cap as 200", () => {
      // Arrange / Act
      const cap = MAX_ALERTS_SHOWN;

      // Assert
      expect(cap).toBe(200);
    });

    it("shows an alert returned again inside the overlap window once, with its latest values", async () => {
      // Arrange
      const original = alertFixture({ alert_id: "a1", observed: 0.31, updated_at: "2026-11-05T09:59:30Z" });
      const repeated = alertFixture({ alert_id: "a1", observed: 0.31, updated_at: "2026-11-05T09:59:30Z" });
      const fresh = alertFixture({ alert_id: "a2", updated_at: "2026-11-05T10:00:10Z" });
      let call = 0;
      const harness = signedIn(() => {
        call += 1;
        return call === 1
          ? page("2026-11-05T10:00:00Z", [original])
          : page("2026-11-05T10:00:15Z", [fresh, repeated]);
      });

      // Act
      harness.renderUi(<AlertsPanel pollMs={POLL_MS} />);
      await waitFor(() => expect(renderedIds()).toEqual(["a2", "a1"]));

      // Assert
      expect(screen.getAllByTestId("alert-item")).toHaveLength(2);
      expect(harness.sent("GET", ALERTS_PATH).length).toBeGreaterThanOrEqual(2);
    });

    it("never shows more than 200 alerts and keeps the ones updated most recently", async () => {
      // Arrange: 210 alerts whose updated_at grows with the index
      const harness = signedIn(() => page("2026-11-06T00:00:00Z", numberedAlerts(0, 210)));

      // Act
      harness.renderUi(<AlertsPanel pollMs={60_000} />);
      await screen.findAllByTestId("alert-item");

      // Assert
      expect(renderedIds()).toEqual(newestFirstIds(10, 210));
    });

    it("drops the oldest alerts when later polls push the list past 200", async () => {
      // Arrange
      let call = 0;
      const harness = signedIn(() => {
        call += 1;
        return call === 1
          ? page("2026-11-06T00:00:00Z", numberedAlerts(0, 200))
          : page("2026-11-06T00:00:15Z", numberedAlerts(200, 203));
      });

      // Act
      harness.renderUi(<AlertsPanel pollMs={POLL_MS} />);
      await waitFor(() => expect(renderedIds()).toEqual(newestFirstIds(3, 203)));

      // Assert
      expect(screen.getAllByTestId("alert-item")).toHaveLength(200);
    });
  });

  describe("default interval", () => {
    async function settle(): Promise<void> {
      for (let i = 0; i < 20; i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
      }
    }

    async function advance(ms: number): Promise<void> {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms);
      });
    }

    it("polls every 15 seconds by default and stops after unmount", async () => {
      // Arrange
      vi.useFakeTimers();
      const harness = signedIn(() => page("2026-11-05T10:00:00Z", []));

      // Act
      harness.renderUi(<AlertsPanel />);
      await settle();
      const afterMount = harness.sent("GET", ALERTS_PATH).length;
      await advance(14_999);
      const justBefore = harness.sent("GET", ALERTS_PATH).length;
      await advance(1);
      await settle();
      const atInterval = harness.sent("GET", ALERTS_PATH).length;
      cleanup();
      await advance(60_000);
      const afterUnmount = harness.sent("GET", ALERTS_PATH).length;

      // Assert
      expect(afterMount).toBe(1);
      expect(justBefore).toBe(1);
      expect(atInterval).toBe(2);
      expect(afterUnmount).toBe(2);
    });
  });
});
