import { expect, test } from "@playwright/test";
import { demoMerchantKey, openMerchantArea, setSecretValue, signInWithKey } from "./support/merchant";
import { expectNoBrowserStorage } from "./support/user";

/** scripts/demo.py SQ1_QUESTION, verbatim. */
const SQ1_QUESTION =
  "15% of my card payments are failing this afternoon — what is the root cause and how do we fix it?";
const ASSISTANT_TIMEOUT_MS = 40_000;

test.describe("merchant area", () => {
  test("the demo key loads the payments panel with rows or the explicit empty state", async ({ page }) => {
    // Arrange
    const key = demoMerchantKey();
    await openMerchantArea(page);

    // Act
    await signInWithKey(page, key);

    // Assert
    const payments = page.getByRole("region", { name: "Payments" });
    await expect(payments.getByRole("heading", { name: "Payments" })).toBeVisible();
    const firstRow = payments.getByTestId("payment-row").first();
    const emptyState = payments.getByText("No payments.", { exact: true });
    await expect(firstRow.or(emptyState)).toBeVisible();
    await expect(payments.getByRole("alert")).toHaveCount(0);
    if ((await emptyState.count()) === 0) {
      await expect(firstRow).toContainText(/captured|declined|partially_refunded|refunded|disputed/);
      await expect(firstRow.getByRole("button", { name: "Open" })).toBeVisible();
    }
  });

  test("the key never reaches the URL or browser storage", async ({ page }) => {
    // Arrange
    const key = demoMerchantKey();
    await openMerchantArea(page);

    // Act
    await signInWithKey(page, key);
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();

    // Assert
    expect(page.url().includes(key)).toBe(false);
    await expectNoBrowserStorage(page);
  });

  test("Investigate failures answers the SQ1 question with at least one evidence item", async ({ page }) => {
    // Arrange
    const key = demoMerchantKey();
    await openMerchantArea(page);
    await signInWithKey(page, key);
    const assistant = page.getByRole("region", { name: "Assistant" });
    await expect(assistant).toBeVisible();

    // Act
    await assistant.getByLabel("Mode").selectOption({ label: "Investigate failures" });
    await assistant.getByLabel("Question").fill(SQ1_QUESTION);
    await assistant.getByRole("button", { name: "Ask", exact: true }).click();

    // Assert
    const answer = assistant.getByTestId("assistant-answer");
    await expect(answer).toBeVisible({ timeout: ASSISTANT_TIMEOUT_MS });
    await expect(answer).toContainText(/Mode: (LLM|deterministic fallback)/);
    await expect(answer.getByText("No evidence returned.")).toHaveCount(0);
    const evidence = answer.getByTestId("evidence-item");
    await expect(evidence.first()).toBeVisible();
    await expect(evidence.first().locator("code")).toHaveText(/^[a-z_]+:\S+$/);
    await expect(assistant.getByRole("alert")).toHaveCount(0);
  });

  test("signing out returns to the key form and drops the session", async ({ page }) => {
    // Arrange
    const key = demoMerchantKey();
    await openMerchantArea(page);
    await signInWithKey(page, key);
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();

    // Act
    await page.getByRole("button", { name: "Sign out" }).click();

    // Assert
    await expect(page.getByRole("heading", { name: "Merchant sign-in (demo)" })).toBeVisible();
    await expect(page.getByLabel("API key")).toHaveValue("");
    await expect(page.getByRole("region", { name: "Payments" })).toHaveCount(0);
    await expect(page.getByRole("region", { name: "Assistant" })).toHaveCount(0);
  });

  test("an invalid key shows an alert and stays on the key form", async ({ page }) => {
    // Arrange
    await openMerchantArea(page);

    // Act
    await signInWithKey(page, "invalid-demo-key-for-e2e");

    // Assert
    await expect(page.getByRole("alert")).toHaveText("The merchant API key was not accepted.");
    await expect(page.getByRole("heading", { name: "Merchant sign-in (demo)" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Payments" })).toHaveCount(0);
  });

  test("submitting a blank key sends no API request and stays on the key form", async ({ page }) => {
    // Arrange
    const apiRequests: string[] = [];
    page.on("request", (request) => {
      if (new URL(request.url()).pathname.startsWith("/api/")) apiRequests.push(request.url());
    });
    await openMerchantArea(page);
    await setSecretValue(page.getByLabel("API key"), "   ");

    // Act
    await page.getByRole("button", { name: "Continue" }).click();

    // Assert
    await expect(page.getByRole("heading", { name: "Merchant sign-in (demo)" })).toBeVisible();
    await expect(page.getByRole("alert")).toHaveCount(0);
    expect(apiRequests).toEqual([]);
  });
});
