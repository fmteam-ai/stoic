import { test as setup, expect } from "@playwright/test";
import { ADMIN_EMAIL, ADMIN_PASSWORD, ADMIN_TOTP_SECRET } from "./helpers";
import { totpCode } from "./totp";

setup("authenticate as admin", async ({ page }) => {
  await page.goto("/login");
  await page.getByTestId("login-email-input").fill(ADMIN_EMAIL);
  await page.getByTestId("login-password-input").fill(ADMIN_PASSWORD);
  await page.getByTestId("login-submit-button").click();

  // Admin MFA on (CI enrols a TOTP secret for the per-run admin): the backend answers
  // "2FA code required", the form shows the code field — compute the code like an
  // authenticator app would and submit again.
  const twoFa = page.getByTestId("login-2fa-input");
  const outcome = await Promise.race([
    page.getByTestId("dashboard-header").waitFor({ state: "visible", timeout: 30_000 }).then(() => "dashboard"),
    twoFa.waitFor({ state: "visible", timeout: 30_000 }).then(() => "2fa"),
  ]);
  if (outcome === "2fa") {
    expect(ADMIN_TOTP_SECRET, "login asked for a 2FA code but E2E_ADMIN_TOTP_SECRET is not set").toBeTruthy();
    await twoFa.fill(totpCode(ADMIN_TOTP_SECRET));
    await page.getByTestId("login-submit-button").click();
  }
  await expect(page.getByTestId("dashboard-header")).toBeVisible({ timeout: 30_000 });
  await page.context().storageState({ path: "playwright/.auth/admin.json" });
});
