// Credentials come from the environment ONLY (audit round 7 P1 — no literals in
// source). The CI e2e job seeds the backend with a per-run ADMIN_PASSWORD and
// passes it as E2E_ADMIN_PASSWORD; locally export both variables.
export const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL || "admin@stoicaibot.com";
export const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD || "";
if (!ADMIN_PASSWORD) {
  throw new Error("E2E_ADMIN_PASSWORD is required (env-provided test admin credential; defaults are not allowed)");
}
