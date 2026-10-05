// Credentials come from the environment ONLY (audit round 7 P1 — no literals in
// source). The CI e2e job seeds the backend with a per-run ADMIN_PASSWORD and
// passes it as E2E_ADMIN_PASSWORD; locally export both variables.
export const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL || "admin@stoicaibot.com";
export const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD || "";
// base32 TOTP secret enrolled on the CI admin (scripts/ci_enrol_admin_totp.py) so the
// suite logs in with admin MFA enforced; empty when the target admin has no 2FA.
export const ADMIN_TOTP_SECRET = process.env.E2E_ADMIN_TOTP_SECRET || "";
if (!ADMIN_PASSWORD) {
  throw new Error("E2E_ADMIN_PASSWORD is required (env-provided test admin credential; defaults are not allowed)");
}
