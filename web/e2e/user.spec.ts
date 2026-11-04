import { expect, test } from "@playwright/test";
import { totpCodeForStep, totpStepAt } from "./support/totp";
import {
  expectNoBrowserStorage,
  expectSignedIn,
  fillCredentials,
  logIn,
  newTestUser,
  signUp,
} from "./support/user";

test.describe("user area", () => {
  test("a new user can sign up, log in and sees a zero USD wallet", async ({ page }) => {
    // Arrange
    const user = newTestUser();
    await signUp(page, user);

    // Act
    await logIn(page, user);

    // Assert
    await expectSignedIn(page);
    const usdRow = page.getByTestId("wallet-USD");
    await expect(usdRow.getByRole("cell")).toHaveText(["USD", "$0.00", "$0.00"]);
    await expect(page.getByTestId(/^wallet-/)).toHaveCount(1);
  });

  test("a wrong password is rejected and the user stays on the login form", async ({ page }) => {
    // Arrange
    const user = newTestUser();
    await signUp(page, user);

    // Act
    await logIn(page, { email: user.email, password: `${user.password}-wrong` });

    // Assert
    await expect(page.getByRole("status")).toHaveText("Email or password is incorrect.");
    await expect(page.getByRole("heading", { name: "Log in" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Log out" })).toHaveCount(0);
  });

  test("a duplicate sign-up shows the already-registered message", async ({ page }) => {
    // Arrange
    const user = newTestUser();
    await signUp(page, user);
    await page.getByRole("button", { name: "Create an account" }).click();

    // Act
    await fillCredentials(page, user);
    await page.getByRole("button", { name: "Create account" }).click();

    // Assert
    await expect(page.getByRole("status")).toHaveText("An account with this email already exists.");
  });

  test("no credential reaches localStorage, sessionStorage or cookies after login", async ({ page }) => {
    // Arrange
    const user = newTestUser();
    await signUp(page, user);

    // Act
    await logIn(page, user);
    await expectSignedIn(page);

    // Assert
    await expectNoBrowserStorage(page);
  });

  test("enrolling TOTP signs the user out, then login requires and accepts the code", async ({ page }) => {
    // Arrange
    const user = newTestUser();
    await signUp(page, user);
    await logIn(page, user);
    await expectSignedIn(page);

    // Act: enrol and confirm with the code for the current time step
    await page.getByRole("button", { name: "Set up an authenticator app" }).click();
    const secretElement = page.getByTestId("totp-secret");
    await expect(secretElement).toHaveText(/^[A-Z2-7]{16,}$/);
    const secret = await secretElement.innerText();
    await page.getByLabel("Code", { exact: true }).fill(totpCodeForStep(secret, totpStepAt(Date.now())));
    await page.getByRole("button", { name: "Turn on" }).click();

    // Assert: signed out with the notice
    await expect(page.getByRole("status")).toHaveText(
      "Two-factor authentication is on. Log in again with your code.",
    );
    await expect(page.getByRole("button", { name: "Log out" })).toHaveCount(0);

    // Act: password alone must now ask for the code
    await logIn(page, user);

    // Assert
    await expect(page.getByRole("status")).toHaveText("Enter the code from your authenticator app.");
    const codeField = page.getByLabel("Authenticator code");
    await expect(codeField).toBeVisible();
    await expectNoBrowserStorage(page);

    // Act: the server rejects a replay of the step used to confirm, so use the next step (inside its +-1 window)
    await codeField.fill(totpCodeForStep(secret, totpStepAt(Date.now()) + 1));
    await page.getByRole("button", { name: "Log in", exact: true }).click();

    // Assert
    await expectSignedIn(page);
    await expect(page.getByTestId("wallet-USD")).toBeVisible();
    await expectNoBrowserStorage(page);
  });

  test("a wrong TOTP code does not sign in", async ({ page }) => {
    // Arrange
    const user = newTestUser();
    await signUp(page, user);
    await logIn(page, user);
    await expectSignedIn(page);
    await page.getByRole("button", { name: "Set up an authenticator app" }).click();
    const secret = await page.getByTestId("totp-secret").innerText();
    await page.getByLabel("Code", { exact: true }).fill(totpCodeForStep(secret, totpStepAt(Date.now())));
    await page.getByRole("button", { name: "Turn on" }).click();
    await expect(page.getByRole("status")).toHaveText(
      "Two-factor authentication is on. Log in again with your code.",
    );
    await logIn(page, user);
    const codeField = page.getByLabel("Authenticator code");
    await expect(codeField).toBeVisible();

    // Act: a code from a step far outside the accepted window (+-1) cannot match
    await codeField.fill(totpCodeForStep(secret, totpStepAt(Date.now()) + 10));
    await page.getByRole("button", { name: "Log in", exact: true }).click();

    // Assert
    await expect(page.getByRole("status")).toHaveText("Email or password is incorrect.");
    await expect(page.getByRole("button", { name: "Log out" })).toHaveCount(0);
  });
});
