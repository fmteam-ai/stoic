# Security Audit #35 — v1.60.44 / v1.60.45 diff + regression sweep

**Verdict: PASS — NO MAJOR ISSUE FOUND (no P0–P2).** Scope: the Scalp page metrics/decisions scoping
(`symbol` + `account_id` + `setup` params, per-setup breakdown), the `/api/scalp/status` snapshot fix, the admin
EA-fleet endpoint/panel, plus a sweep of auth/JWT, admin gating, bridge-token auth, CORS, secrets handling and the
release/deploy scripts (`verify_ea_release.py`, `check_ea_fleet_compat.py`, `deploy/lib.sh`, `deploy/update.sh`).
Live unauthenticated probes: `/api/ops/ea-fleet` → 403; `/api/scalp/status|metrics|decisions` → 401.

## Verified controls
- `scalp_routes.py` status/decisions/metrics — every query is seeded with `{"user_id": user["id"]}`; `account_id`,
  `symbol`, `setup` are typed `str` and appended as plain equality values (no Mongo operator injection); a foreign
  `account_id` returns an empty set, never another tenant's rows. `status()` iterates a `list()` snapshot and skips
  runners whose `user_id` differs. Sample sizes bounded (`to_list(5000)`, decisions ≤ 200).
- `Scalp.jsx`, `EaFleetCards.jsx` — DB-sourced setup names / EA labels / command errors render through JSX text
  interpolation (auto-escaped). The only `dangerouslySetInnerHTML` uses in the app go through `DOMPurify.sanitize`.
- `GET /api/ops/ea-fleet` — `_ops_actor` (METRICS_TOKEN or admin **and** enrolled 2FA); exposes public EX5 SHA-256
  hashes, versions, labels and counts only — no signing-key material.
- `auth.py` — JWT pinned to HS256 with explicit `algorithms=[...]`, type/iat/session-revocation/status checks.
  `server.py` — credentialed CORS only for the filtered allowlist.
- Release/deploy scripts — `subprocess` in argument-list form (no `shell=True`); shell expansions quoted; inputs are
  local `.env`/git values, not request data.

## Hardening (applied)
- `scalp_routes.py` decisions: `limit` clamped `min(max(int(limit), 1), 200)` (negative values previously reached Motor).
- `bridge_routes.py`: removed the unused `trade_reconciler.reconcile_user` import (pre-existing F401).

## Not exercised
- Credentialed cross-tenant runtime flows (read-only audit policy); covered by source review of the `user_id` scoping.
