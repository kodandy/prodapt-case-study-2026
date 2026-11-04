import { expect, type Locator, type Page } from "@playwright/test";

/** The demo key is injected by `make web-smoke`; a missing value must fail loudly, never skip. */
export function demoMerchantKey(): string {
  const key = process.env["WEB_DEMO_MERCHANT_KEY"];
  if (key === undefined || key.trim() === "") {
    throw new Error("WEB_DEMO_MERCHANT_KEY is not set; run these tests through `make web-smoke`");
  }
  return key.trim();
}

/**
 * Sets a controlled input without Playwright's fill(): fill's value would appear in call logs, error
 * messages and traces. The value is passed as an evaluate argument, which Playwright does not print.
 */
export async function setSecretValue(input: Locator, value: string): Promise<void> {
  await input.evaluate((element, secret) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (setter === undefined || !(element instanceof HTMLInputElement)) throw new Error("not a text input");
    setter.call(element, secret);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  }, value);
}

export async function openMerchantArea(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByRole("button", { name: "Merchant", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Merchant sign-in (demo)" })).toBeVisible();
}

export async function signInWithKey(page: Page, key: string): Promise<void> {
  await setSecretValue(page.getByLabel("API key"), key);
  await page.getByRole("button", { name: "Continue" }).click();
}
