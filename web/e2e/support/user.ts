import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";

export interface TestUser {
  email: string;
  password: string;
}

/** A fresh identity per call; the .invalid TLD guarantees it can never be a real mailbox. */
export function newTestUser(): TestUser {
  return { email: `e2e-${randomUUID()}@example.invalid`, password: `Pw-${randomUUID()}` };
}

export async function signUp(page: Page, user: TestUser): Promise<void> {
  await page.goto("/");
  await page.getByRole("button", { name: "Create an account" }).click();
  await page.getByLabel("Email").fill(user.email);
  await page.getByLabel("Password", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("status")).toHaveText("Account created. Log in.");
}

export async function fillCredentials(page: Page, user: TestUser): Promise<void> {
  await page.getByLabel("Email").fill(user.email);
  await page.getByLabel("Password", { exact: true }).fill(user.password);
}

export async function logIn(page: Page, user: TestUser): Promise<void> {
  await fillCredentials(page, user);
  await page.getByRole("button", { name: "Log in", exact: true }).click();
}

export async function expectSignedIn(page: Page): Promise<void> {
  await expect(page.getByRole("button", { name: "Log out" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Wallets" })).toBeVisible();
}

/**
 * Credentials must live in memory only (docs/specs/web.md decision 4). The checks are string expressions
 * because the project lint rules forbid referencing the storage globals in code.
 */
export async function expectNoBrowserStorage(page: Page): Promise<void> {
  const localCount = await page.evaluate<number>("localStorage.length");
  const sessionCount = await page.evaluate<number>("sessionStorage.length");
  const cookieString = await page.evaluate<string>("document.cookie");
  const contextCookies = await page.context().cookies();
  expect({ localCount, sessionCount, cookieString, contextCookieCount: contextCookies.length }).toEqual({
    localCount: 0,
    sessionCount: 0,
    cookieString: "",
    contextCookieCount: 0,
  });
}
