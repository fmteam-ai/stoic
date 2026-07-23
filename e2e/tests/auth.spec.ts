import { test, expect } from "@playwright/test";
import { ADMIN_EMAIL } from "./helpers";

test.describe("authentication", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("rejects invalid credentials", async ({ page }) => {
    await page.goto("/login");
    await page.getByTestId("login-email-input").fill(ADMIN_EMAIL);
    await page.getByTestId("login-password-input").fill("definitely-wrong-pw");
    await page.getByTestId("login-submit-button").click();
    await expect(page.getByTestId("login-error")).toBeVisible();
  });

  test("protected route redirects unauthenticated visitor away", async ({ page }) => {
    await page.goto("/trades");
    await page.waitForURL("**/welcome", { timeout: 30_000 });
    await expect(page).toHaveURL(/\/welcome/);
  });
});

test.describe("authenticated session", () => {
  test("admin session reaches the dashboard", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("dashboard-header")).toBeVisible({ timeout: 30_000 });
  });
});
