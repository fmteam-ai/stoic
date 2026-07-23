import { test as setup, expect } from "@playwright/test";
import { ADMIN_EMAIL, ADMIN_PASSWORD } from "./helpers";

setup("authenticate as admin", async ({ page }) => {
  await page.goto("/login");
  await page.getByTestId("login-email-input").fill(ADMIN_EMAIL);
  await page.getByTestId("login-password-input").fill(ADMIN_PASSWORD);
  await page.getByTestId("login-submit-button").click();
  await expect(page.getByTestId("dashboard-header")).toBeVisible({ timeout: 30_000 });
  await page.context().storageState({ path: "playwright/.auth/admin.json" });
});
