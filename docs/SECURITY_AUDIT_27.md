# Security Audit #27 — Cap-Aware Presets, Demo-Autopilot attested gate, v1.60.30 release items, deployed-app perimeter

Scope: every surface changed since audit #26 (`docs/SECURITY_AUDIT_26.md`): cap-aware strategy presets
(`routes/bot_routes.py` `preset_cap_fit` / `get_strategy_presets` / `apply_strategy_preset` / `get_config`,
`user_presets.py`, `frontend/src/pages/BotConfig.jsx` CapFitPill); the Demo Autopilot execution gate
(`operational_modes.mode_gate` → `broker_env.attested_environment`); the v1.60.30 release items
(`account_routes.connection_hint`, `POST /api/ops/dependency-audit`, `scripts/publish_dependency_audit.py`,
`deploy/Caddyfile` conditional headers, docker-compose read-only backup mount); plus a READ-ONLY unauthenticated perimeter
sweep of the DEPLOYED app (https://stoicaibot.com) compared with the preview. Source traced end-to-end; no mutating
request, no credential used, no secret printed or committed.

**Verdict: PASS — no P0/P1/P2. Two P3 hardening notes, both accepted / by design.**

| ID | Sev | Location | Finding | Status |
|---|---|---|---|---|
| SEC-027-1 | P3 | `GET /api/infra/artifacts/manifest` | Unauthenticated manifest exposes the EA `.mq5` source URL and version history. Intended client-distribution surface, Ed25519-signed with per-artifact sha256 and content-addressed URLs; agents verify before install. | ACCEPTED — by design (gate behind enrollment only if the EA source is ever meant to be confidential). |
| SEC-027-3 | P3 | `deploy/cloudflare/render.sh` | The S5 edge security-header block was added only to `deploy/Caddyfile`; production runs the Cloudflare overlay whose Caddyfile is rendered by this script, so the edge guarantee was absent there (headers still came from nginx in the frontend image). | FIXED post-audit — renderer now emits the identical `header { ?Strict-Transport-Security … ?Content-Security-Policy … }` block; `test_edge_sets_security_headers_…` renders it offline and asserts byte-identical header sets. |
| SEC-027-2 | P3 | `GET /api/status` (public) | 12-char inventory digest + authority version (carried from #25). | ACCEPTED — non-sensitive. |

## Confirmed safe
- Cap-aware presets IDOR: `GET /api/bot/presets?account_id=` (`bot_routes.py` ~1148-1159) does not verify ownership in
  `_resolve_account_scope`, but `_config_filter` (~30-40) always pins `user_id`, so a foreign `account_id` matches no
  document → empty cfg → `cap_fit` is computed only from the caller's own (empty) config + the global policy cap. No
  foreign config, existence or risk level leaks. `apply_strategy_preset`, `get_config` and `update_config` all re-check
  account ownership (generic 404 otherwise).
- Preset risk-cap refusal cannot be bypassed: `_refuse_risk_above_policy_cap` recomputes effective risk via the same
  `effective_risk_for` arithmetic as the UI pill and also blocks adaptive-cap raises; the 409 body returns only the
  caller's own cap / effective values.
- Custom-preset property-level authorization: `user_presets.create_user_preset` → `_sanitize_config` strips the stored
  `config` to a behaviour-knob whitelist, so `active`, `user_id`, `account_id`, `operational_mode`, risk fields and
  `$`-operators can never reach the `$set` overlay in `apply_strategy_preset`. Built-in preset configs are
  server-defined. No privilege escalation, no NoSQL-operator injection.
- `BotConfig.jsx`: CapFitPill and all preset name / description renders use JSX text and `title={...}` interpolation;
  React auto-escapes, no `dangerouslySetInnerHTML`. No XSS sink from user-authored preset strings.
- Demo-autopilot gate: `operational_modes.mode_gate` classifies via `broker_env.attested_environment` and fails closed to
  LIVE (no execution) unless PAPER, or a DEMO attestation that is admin-approved, identity-hash-bound,
  verifier-allowlisted AND `broker_environment == DEMO`, with any broker-reported real / contest trade mode voiding it.
  A forged DEMO attestation on a live account cannot execute.
- `POST /api/ops/dependency-audit`: gated by `_ops_actor` (METRICS_TOKEN constant-time `hmac.compare_digest`, fail-closed
  503 when unset, or admin + 2FA), 256 KiB body cap, ≤ 500 vulns, every stored string length-bounded. No auth bypass /
  DoS; the write requires privilege so there is no unauthenticated stored-XSS vector.
- `connection_hint` on GET /accounts: owner-scoped (`list_accounts` filters by `user_id`), returns only the public
  WebRequest server URL + a generic hint; no token / secret / cross-tenant leak.
- Perimeter (prod https://stoicaibot.com): HSTS (preload on API), strict CSP with `frame-ancestors 'none'`,
  X-Frame-Options DENY, nosniff, Referrer-Policy, COOP / CORP, Permissions-Policy all present; `/docs`, `/openapi.json`,
  `/redoc`, `/metrics`, `/.env`, `/.git/config` all return the SPA `index.html` (FastAPI docs not mounted); a foreign
  `Origin` gets no `Access-Control-Allow-Origin` / credentials reflection; malformed JSON to `/api/auth/login` returns a
  generic 422 with no stack or secret. Caddyfile sets headers conditionally (only when upstream lacks them); backup
  mount is `:ro`.

## Coverage and limits
- Reviewed: bot_routes preset / cap paths, user_presets, operational_modes + broker_env demo gate, ops dependency-audit +
  metrics auth, account connection_hint, BotConfig.jsx sinks, Caddyfile / compose, and an unauthenticated prod + preview
  perimeter sweep (headers, CORS, docs / source / secret exposure, EA artifacts, malformed-JSON error body).
- No authenticated mutating session was exercised (source + read-only probes), consistent with prior baselines; cookie
  flags on auth endpoints were taken from source + audit #26.
- Accepted from earlier audits still valid: #25 `/status` digest; #26 SEC-026-2 `HEAD~20` fallback (backstopped by the
  full-history scanner step).
