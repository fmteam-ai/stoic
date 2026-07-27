// Must match seed_admin()'s default (backend/seed.py) so a FRESH CI database
// authenticates: no ADMIN_EMAIL/E2E_ADMIN_EMAIL is set in the e2e workflow, so
// the seeded admin is admin@stoicaibot.com / admin123. Override via env locally.
export const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL || "admin@stoicaibot.com";
export const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD || "admin123";
