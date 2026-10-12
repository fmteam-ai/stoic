# main128 — fixes for the main127 review (Oct 11 2026, @george) — EA 1.64 / installer 1.9 unchanged

Verdict addressed: **NO-GO on main127** → every High and Medium item below is fixed in code with a unit test; the
"still open from earlier reviews" list is handled where it is code-fixable (see the last table). No `--force`
push; `release/ea_release.json` untouched. `release/rc_lock.json` was re-frozen with `scripts/freeze_rc_lock.py`
ONLY because `tests/unit/test_rc_lock_manifest_bound.py` binds it to `docs/TEST_MANIFEST.md` (new test file) —
the changed fields are exactly `test_manifest_sha256 / captured_at / git_commit / source_sha`; the
`ea-release` workflow re-captures the lock on `main` anyway.

## High

| # | Finding | Fix | Test |
|---|---|---|---|
| H1 | One user over the per-user cap put the whole platform in close-only | `inventory_projection.projection` now returns `per_user_blocks {user_id: [reasons]}` (cap, not-admitted, not-DEMO) and `platform_violations` (everything else); `blocking` / `approvable` are computed from the platform list only. `trading_authority.inventory_domain(account)` returns CLOSE_ONLY for the **breaching user's accounts only** (`"rules policy breach for this user: …"`); other users stay FULL. | `test_phase3_rules_policy::test_u14_…` (u1 CLOSE_ONLY, u2 FULL, platform not blocking) |
| H2 | `/bot/start` with no `account_id` skipped the readiness gate; `/bot/stop` with no id stopped one guessed account | Start: the default scope is resolved **first** (`_resolve_account_scope`) and the readiness gate runs on the account that will actually be started. Stop without id = **stop-all** for the caller (`update_many user_id+active`), returns `stopped_account_ids`. | `test_main127_review::test_h2_*` (source order + runtime stop-all isolation) |
| H3 | Bridge auto-adopted the server name before checking who sent the heartbeat (revoked/unknown installations, grace tokens, two terminals flip-flopping) | Adoption moved **after** `verify_heartbeat_identity`; runs only when the chain failed on the server link alone, with a **FULL (current) token** (`acc["_token_kind"]`), and `server_adopt.identity_passes_except_server` re-checks active installation bound to this account + same login + lease. The adoption **never changes this heartbeat's identity result**. Manual-edit lock (24 h, sources `owner_edit`/`ea_reported`) and a flip-flop guard (6 h) that files security finding `server_flipflop` instead of adopting. | `test_phase1_onboarding::test_u2_adopt_writes_and_audits`, `test_u2_heartbeat_adopts_only_after_identity_chain_with_full_token` |
| H4 | `update.sh` died silently (grep under `set -euo pipefail`); `release_precheck.py` only accepted `event=push` | Every `.env` lookup is `… \|\| true`; missing repo slug prints a clear message and continues to the attestation gate. `release_precheck.release_run` queries all events, accepts `push` / `release` / `workflow_dispatch`, prefers a green run; `OSError`/`KeyError` → exit 4 (no traceback). | `test_main127_review::test_h4_*` |

## Medium

| # | Finding | Fix | Test |
|---|---|---|---|
| M1 | Rules policy not tied to the installation when `STOIC_INSTALLATION_ID` unset | `rules_policy.problems`: in production an unset id is a hard error ("fail closed"); preview unchanged. | `test_m1_production_without_installation_id_fails_closed` |
| M2 | Risk caps never enforced (`risk_per_trade_pct` is never saved; `max_daily_loss_pct` used nowhere) | `rules_policy.effective_max_risk_pct(cfg)` = profile `risk_pct` by `risk_level`, lifted to `adaptive_risk_cap_pct` when adaptive sizing may scale up — admission compares **that**. Runtime: `bot_runner` sizing clamps `profile.risk_pct` to `max_risk_pct` (`signal.policy_risk_clamp`); `circuit_breakers.check_and_trip` tightens the daily limit to `max_daily_loss_pct` and forces the daily breaker on (`policy_daily_cap_pct` in the result). | `test_m2_*` (admission, runtime daily-loss trip) |
| M3 | GET `/authority/inventory` wrote approval events | `projection()` is read-only (`auto_approval_due` flag only). New `auto_approve_under_rules(db)` is the write step, called from the heartbeat admission hook, account creation, `/bot/start` and the 60 s `_heartbeat_watch_loop` sweep. | `test_u14_…` (`insert_one` absent from `projection`, all four call sites present) |
| M4 | Startup migration deleted ACTIVE bot configs | `bot_inventory.delete_orphan_bot_configs`: active orphans are **never deleted** (counted, warned, inventory reports them); inactive orphans move to `bot_configs_orphaned` (restorable) with reason + timestamp; audit action `bot_configs_orphans_quarantined`. | `test_u11_no_account_less_configs_and_orphan_migration` |
| M5 | Auto-adopt undid manual edits within ~5 s | Covered by the H3 manual-edit lock (24 h). | `manual_lock_active` cases in `test_u2_adopt_writes_and_audits` |
| M6 | Manual server edit reached only the admin's own accounts | `PATCH /accounts/{id}`: admin check first; admins reach any account (audit carries `owner_user_id`/`on_behalf`), users only their own. | `test_u3_server_edit_admin_only` |

## Still open from earlier reviews — status

| Item | Status |
|---|---|
| Auto-attest grant is never re-checked | **FIXED** — `auto_attest.evaluate` re-runs every check on every heartbeat for an automatic grant; identity moved / unsigned binary / broker no longer demo / proof failing → voided + audited (`recheck_failed`). |
| Operator approval isn't tied to identity | **FIXED** — `operator_approve` stores `identity_hash = attestation_identity(acc)`; `sync_admission` drops an operator approval whose identity hash no longer matches back to `pending_operator` ("re-approval needed"). |
| A rules policy can't stand in for the expectation | **FIXED** — `inventory_domain` / `projection` treat an active, unexpired signed rules policy as the expectation (no "no inventory expectation declared" close-only in production while a rules policy is active). |
| Auto-DEMO grant forgeable by whoever holds the token (heartbeat `trade_mode`) | **OPEN (waived P1-01-r20-ea-proof)** — needs the EA-side binary proof (EX5 hash reported by the terminal and verified against the signed release); EA/installer change, not in this zip. |
| Repair leaves a duplicate EA or stale `BridgeToken` input | **OPEN** — installer-side (`STOIC-Installer.ps1`); scheduled with the next EA/installer release. |
| P3 items | unchanged |

## Changed files
backend: `inventory_projection.py`, `trading_authority.py`, `rules_policy.py`, `server_adopt.py`, `bot_inventory.py`,
`auto_attest.py`, `circuit_breakers.py`, `bot_runner.py`, `background_loops.py`, `routes/bridge_routes.py`,
`routes/bot_routes.py`, `routes/account_routes.py`, `security_agent/playbooks.py`;
deploy/scripts: `deploy/update.sh`, `scripts/release_precheck.py`;
tests: `tests/unit/test_main127_review.py` (new, 8), `test_phase1_onboarding.py`, `test_phase3_rules_policy.py`,
`test_fixplan_a16_part1.py`, `fake_mongo.py` (insert_many, sort);
artefacts: `docs/TEST_MANIFEST.md` (5598 tests / 591 files), `docs/RELEASE_SUMMARY.md`, `release/rc_lock.json` (manifest hash only).

## Test results
`DB_NAME=ai_trading_bot_test python -m pytest tests/unit` → **1322 passed, 4 skipped** (unit lane), `ruff` clean on every
touched module, `bash -n deploy/update.sh` OK, `check_deploy_python_snippets.py` OK (43 snippets / 3.6 syntax),
`freeze_rc_lock.py --check` OK. Live preview: `GET /api/authority/inventory` read-only (returns
`platform_violations` / `per_user_blocks` / `auto_approval_due`), `POST /api/bot/stop` without id returns
`stopped_account_ids` for every owned account.
