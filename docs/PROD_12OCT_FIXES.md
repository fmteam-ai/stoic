# Production issues 12 Oct 2026 (stoicaibot.com, v1.60.21) — fixes

| # | Issue | Fix | Test |
|---|-------|-----|------|
| 1 | Rules-policy upload 422 "signature does not verify" for whole-number float risk caps (`5.0` → browser `JSON.parse` → `5`; `body()` re-serialised `"5"`, signer signed `"5.0"`) | `RulesPolicyPanel.jsx` sends the pasted file as raw text (`policy_text`; syntax-checked client-side only); `POST /api/authority/policy/upload` parses it with Python `json.loads` (float-preserving), 422 `policy_not_json` / `policy_not_object`, 413 over 64 KiB; dict payloads still accepted for scripts. | `test_1_browser_json_parse_loses_the_float…` (reproduces the prod 422), `test_1_upload_route_accepts_the_raw_text_and_activates` (`risk_caps {max_daily_loss_pct: 5.0}` through the route) |
| 1b | "Load latest signed policy" silently found nothing without `GITHUB_REPO` | `load_latest` returns `reason: "repo not configured"`, `repo_configured`, `note`; renewal payload carries `repo_configured`; panel shows a "repo not configured" badge next to the button and a warning toast. | `test_1_load_latest_reports_repo_not_configured` |
| 2 | 502 for ~3 min after `deploy/restart.sh --env-changed` (nginx resolved `backend` once; backend recreated with a new IP, frontend not) | `deploy/nginx.conf`: `resolver 127.0.0.11 valid=10s ipv6=off; set $backend http://backend:8001; proxy_pass $backend;` (same path + header behaviour; `nginx -t` OK). `restart.sh` now also waits for `GET /api/health` = 200 **through the frontend** (`wait_api_health_via_frontend`, 90 s) before reporting success. | `test_2_nginx_re_resolves_the_backend_and_restart_waits_through_the_frontend` |
| 3 | `update.sh` refused every production update: `strict_prebuild_gate` ran `verify_ea_release.py --check` in the host shell without `RELEASE_PUBLIC_KEY_B64 / RELEASE_SIGNER_KEY_ID / RELEASE_SIGNER` → `revoked-or-unknown` | `verify_ea_release.py --check` reads exactly those three pins from `backend/.env` itself (`load_backend_env_pins`; never printed, an exported value wins; `STOIC_BACKEND_ENV_FILE` override). Verified against GitHub main's EA v1.64 record: `env -i` → FAIL before, `OK … signature verified (key stoic-release-ed25519-v2)` after. | `test_3_verify_ea_release_reads_the_three_pins_from_backend_env`, `test_3_gate_runs_in_an_empty_shell_environment` (`env -i` subprocess) |
| 3b | `./.env` on prod contains `ATTESTATION_REQUIRED=false` (written by `bootstrap.sh --skip-attestation`) | `bootstrap.sh`: on public hosts (`--production/--behind-proxy/--cloudflare`) the flag is exported for THAT bootstrap run only and any `ATTESTATION_REQUIRED=` line is removed from `./.env`; `--dev` unchanged. **Yes — remove the line on prod**: releases are attested (`attested release 4baae3eb`), `DEPLOY_MODE` auto/registry and `update.sh` verify the attestation of the tag being deployed; with the line gone the gate is required again (fail-closed), which is the intended production posture. | `test_3_bootstrap_never_writes_attestation_required_false_on_public_hosts` |

Full lane: unit 1381 ✓ · frontend lint/typecheck/test ✓ · manifest 5664/597 · rc_lock refrozen by `freeze_rc_lock.py` only · `ea_release.json` untouched · release summary + provenance checks OK.

Operator steps on prod after deploying this build: `sed -i '/^ATTESTATION_REQUIRED=/d' .env` (then `deploy/update.sh <tag>` verifies the attestation), set `GITHUB_REPO=fmteam-ai/stoic` in `backend/.env` and `deploy/restart.sh --env-changed` (now waits for health through nginx), re-upload `demo-rules-v1` via the panel.

## Item 4 (main132 follow-up) — demo-only production can leave close-only
`backend/ops/prepromotion_evidence.py` now reads the ACTIVE rules policy (`rules_policy.active`). Under a `demo_only`
policy ("demo period") the environment gate becomes `all_enabled_attested_demo` — every enabled account must be an
**attested** DEMO account (`broker_env.attested_environment`, server-authoritative; a LIVE account or an unattested
demo-looking account FAILS) — and the LIVE-only performance attestation is the ONE gate skipped
(`performance_gate_reasons: ["SKIPPED_DEMO_PERIOD"]`). Every other gate (topology, EA fresh, identity verified, single EA
version, zero unresolved executions, position truth, authority, bot-health caps) is unchanged. The bundle carries
`regime` + `evidence_label: "demo period"`; `update.sh` prints the label. LIVE regime (no policy / `demo_only: false`)
behaves exactly as before (`all_enabled_live` + performance attestation).
Tests `tests/unit/test_item4_demo_period_evidence.py` (5): demo_only + attested DEMO → PASS; demo_only + LIVE → FAIL;
demo period still fails on other gates; LIVE regime unchanged.

## v1.60.22 — Bot Config save 500 (UnboundLocalError) + access-log redaction tracebacks
- Root cause: `routes/bot_routes.py update_config()` re-imported `audit_event` inside the `pattern_gate_enabled` branch; Python
  made the name local for the whole function, so every step-up save (`live_activation` / `risk_raise`, mode promotion /
  demotion) without `pattern_gate_enabled` raised `UnboundLocalError` → 500. Fix: local import deleted (module import at
  line 10 provides it). Same class fixed repo-wide: **56** function-local imports that shadowed a module-level import of the
  identical name were removed across 27 backend files (identical source module only — verified by AST; ruff F821/F811 clean).
- New CI gate: `scripts/check_local_import_shadows.py` (AST, every function in backend/) + ruff `F823` in `static-analysis`.
- `_serialize()` now also returns `pattern_gate_enabled` (the admin toggle was written but invisible in `GET /api/bot/config`).
- `security_agent/redact.py`: `uvicorn.access` records keep their 5-tuple `record.args` (string members masked in place) —
  no more "Logging error … not enough values to unpack (expected 5, got 0)".
- Tests `tests/unit/test_prod_v1_60_22_bot_config_500.py` (5): risk raise and activation on a live-context account without
  `pattern_gate_enabled` → 200 + step-up audit; admin toggle still audited; repo-wide shadow check; access-log args shape.
  Verified live on preview: `PUT /api/bot/config` risk raise → 200, `risk_raise` audit written.
