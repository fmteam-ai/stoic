import { test, expect } from "@playwright/test";

const PAGES = [
  { path: "/trades", testid: "trades-header" },
  { path: "/bot-health", testid: "bot-health-header" },
  { path: "/accounts", testid: "accounts-header" },
];

test.describe("core pages render", () => {
  for (const p of PAGES) {
    test(`renders ${p.path}`, async ({ page }) => {
      await page.goto(p.path);
      await expect(page.getByTestId(p.testid)).toBeVisible({ timeout: 30_000 });
    });
  }
});

test.describe("bot health ops cards", () => {
  test("readiness, alerts, stage and validation cards render for admin", async ({ page }) => {
    await page.goto("/bot-health");
    await expect(page.getByTestId("readiness-card")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("ops-alerts-card")).toBeVisible();
    await expect(page.getByTestId("stage-card")).toBeVisible();
    await expect(page.getByTestId("validation-card")).toBeVisible();
    await expect(page.getByTestId("stage-current")).toBeVisible();
  });
});
