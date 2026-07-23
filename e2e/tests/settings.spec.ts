import { test, expect } from "@playwright/test";

test.describe("settings", () => {
  test("shows profile, password and 2FA sections without bootstrap banner", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByTestId("profile-section")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("password-section")).toBeVisible();
    await expect(page.getByTestId("twofa-section")).toBeVisible();
    await expect(page.getByTestId("must-change-password-banner")).toHaveCount(0);
  });

  test("rejects password change with wrong current password", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByTestId("password-section")).toBeVisible({ timeout: 30_000 });
    await page.getByTestId("pw-current").fill("wrong-current-pw");
    await page.getByTestId("pw-new").fill("Str0ng-N3w-Pass!");
    await page.getByTestId("pw-confirm").fill("Str0ng-N3w-Pass!");
    await page.getByTestId("change-password-button").click();
    await expect(page.getByTestId("pw-error")).toBeVisible();
  });
});
