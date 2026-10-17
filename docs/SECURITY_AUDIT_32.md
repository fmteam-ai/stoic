# Security Audit #32 — v1.60.33 → v1.60.36 release content, deployed-app perimeter

Scope: every surface changed since audit #31 — fill re-base / signal slippage and the `confirmed_position_tp` ack path
(`exit_sync.py`, `routes/bridge_routes.py`), stop guard + refusal trail (`trade_manager.py`, `adaptive_exits.py`),
EA channel promotion/rollback with step-up and audit (`release_channels.py`, `routes/ops_routes.py`), release-gate
digest compare and the deploy-time `evaluate()` check (`release_gate.py`, `deploy/update.sh`, `deploy/lib.sh`),
Strategy Report / patterns dedupe / entry context / auto-apply evidence bar (`strategy_report.py`,
`routes/postmortem_routes.py`, `entry_context.py`, `loss_advisor.py`), failure-classifier backfill gap, the
`regen_release_artifacts.py` CI refusal, and the frontend chunk-reload / new cards. Plus the standing
auth/JWT/CORS/security-header review and a READ-ONLY check of the preview deployment. No mutating request, no secret
printed or committed.

## Verdict

**PASS — NO MAJOR ISSUE FOUND.** No cross-tenant data path, no privilege escalation, no unauthenticated access to
private reports, no fail-open in the release gate. Confidence HIGH.

## Verified (by focus area)

| Area | Result |
|---|---|
| Bridge fill re-base / modification-ack | `trade_id` must belong to the token's account (`bridge_routes.py` /report and /modification-ack scoping). Server-originated stop moves only tighten (`exit_sync.fill_rebase` → `tightened_sl`; Tier-1 combo follow-up re-checked). EA-reported `confirmed_position_sl/tp` affect only that owner's trade record — the EA is already the broker authority for that account (accepted by design). |
| Release promote / rollback | Admin role + enrolled admin 2FA + fresh step-up (`release_promote` / `release_rollback`); machine path uses constant-time `METRICS_TOKEN`. `_channel_audit` appends who/when/from→to to the step-up audit log. No BFLA bypass. |
| Release gate | `evaluate()` fail-closed (every missing fact → `fails`); `_bare_digest` compares the `sha256:` part only; `deploy/update.sh` rolls back on a failing gate in production. |
| Strategy report / patterns / loss advisor | All queries filter by session `user_id`; `days` typed int clamped 1–180 and used only in a timedelta; reads bounded (`to_list(5000)`, `$limit`); auto-apply per-user gated with the new ≥ 20-signal bar. |
| Frontend reload | `lazyImport` reloads at most once per URL (sessionStorage href compare) and only via `window.location.reload()` — no loop, no open redirect. |
| Standing | 401 on unauthenticated API, security headers present on the live preview, `.env` git-ignored and untracked. |

## Hardening (P3, optional)

1. Preview CORS is a credential-less wildcard (`server.py` CORS block) — safe as configured; keep the credentialed
   allowlist populated in production.
2. `routes/bridge_routes.py` modification-ack records the EA-reported `confirmed_position_sl` as-is; consider flagging
   when an ack LOOSENS the stored stop (self-account anomaly signal, defense in depth only).

## Coverage and limits

BOLA/BFLA, authentication, CSRF/CORS, injection, fail-open, bounded reads and audit integrity applied to the changed
paths. Broker-side behaviour of a compromised EA token is outside the platform trust boundary; React cards reviewed via
their data paths; dependency advisory feed not independently re-scanned (covered by the CI requirements guard).
