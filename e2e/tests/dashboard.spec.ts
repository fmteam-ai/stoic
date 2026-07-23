import { test, expect } from "@playwright/test";

test.describe("dashboard", () => {
  test("renders header and bot status strip", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("dashboard-header")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("bot-status-strip")).toBeVisible({ timeout: 30_000 });
  });
});
