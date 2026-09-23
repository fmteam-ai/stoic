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

  test("protected route lands on the login boundary — never the welcome page", async ({ page }) => {
    // audit round 6 P0: protected URLs render exactly one of app · /login · outage screen
    await page.goto("/trades");
    await page.waitForURL("**/login", { timeout: 30_000 });
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByTestId("login-form")).toBeVisible();
    await expect(page.getByTestId("welcome-page")).toHaveCount(0);
  });

  test("root stays the public marketing entrypoint for logged-out visitors", async ({ page }) => {
    await page.goto("/");
    await page.waitForURL("**/welcome", { timeout: 30_000 });
    await expect(page.getByTestId("welcome-page")).toBeVisible();
  });

  test("protected route shows the explicit outage screen when the backend is down", async ({ page }) => {
    await page.route("**/api/auth/me", (route) => route.abort());
    await page.goto("/dashboard");
    await expect(page.getByTestId("backend-outage-screen")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("backend-outage-correlation-id")).not.toBeEmpty();
    await expect(page.getByTestId("welcome-page")).toHaveCount(0);
    await expect(page).not.toHaveURL(/\/welcome/);
  });
});

test.describe("authenticated session", () => {
  test("admin session reaches the dashboard", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("dashboard-header")).toBeVisible({ timeout: 30_000 });
  });
});
