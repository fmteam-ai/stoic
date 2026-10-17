# STOIC Add-ons Build Spec — delivery (2026-10-12)

Scope delivered on top of `main`: Add-on A (prediction track record + calibration), Add-on B (chart-pattern layer,
observe-only), hardening C1–C3, Track record tab, Chart patterns posture row.
NOT built (per spec): funding-rate carry, strategy-DSL backtester, shadow runner.

## Section 8 — Acceptance checklist

| # | Item | Result |
|---|------|--------|
| 1 | `prediction_track.py`, the route and tests A.8 (all 10) pass | PASS — `test_a1`…`test_a10` (10/10) |
| 2 | `chart_patterns.py`, wiring and tests B.12 (all 11) pass; snapshot under 100 ms on 800 bars | PASS — `test_b1`…`test_b11` (11/11); perf asserted in `test_b11` |
| 3 | With `pattern_gate_enabled` false, no trade is skipped by the pattern gate (test 10 + preview pulse check) | PASS — `test_b10_observe_only_wiring`; preview DB: 0 `pattern_gate_veto` counters, 0 pulses with a pattern reason |
| 4 | `GET /api/trades/predictions?symbol=XAUUSD` on preview returns `recent` and `calibration`; 422 on a bad symbol | PASS — 200 with `recent` (6 pending rows) + `calibration` (Wilson ci95, by_confidence, brier); `symbol=../x` → 422 |
| 5 | `/api/bot/posture` shows `chart_patterns` per symbol | PASS — `{ready:false, reason:"insufficient intraday bars", enforced:false}` for the admin account (no M15 stream yet) |
| 6 | C1–C3 each with a test that fails before and passes after | PASS — `test_c1_list_file_is_skipped_and_valid_one_loads`, `test_c2_future_issued_at_rejected`, `test_c3_tombstone_indexes_and_bson_expiry`; preview DB has `token_hashes_1` + `expires_at_1 (expireAfterSeconds 0)` |
| 7 | Both screens at 390 px and desktop (screenshots) | PASS — `docs/screenshots/addons/*.jpg` (4 files) |
| 8 | No new code path places or modifies an order | PASS — diff + new modules grepped for `execute(`, `order_send`, `place_order`, `modify`, `close_position`, `MT5BridgeEngine`, `pending_trades` inserts: none |
| 9 | Manifest regenerated, `rc_lock.json` refrozen (only hash/commit/timestamp changed), unit lane green, ruff clean, CI green | PASS locally — manifest 5636 tests / 594 files; rc_lock diff = `captured_at`, `git_commit`, `source_sha`, `test_manifest_sha256` only; `tests/unit` 1358 passed / 4 skipped; `ruff check backend --select E9,F63,F7,F82` clean. CI runs on push. |

## Test results
- `tests/unit/test_addons_spec.py`: 24 passed (A 10 · B 11 · C 3).
- Full unit lane (`pytest tests/unit`): 1358 passed, 4 skipped.
- `ea_release.json` untouched. No force push.

## Screenshots
- `docs/screenshots/addons/track_record_desktop.jpg` · `track_record_390.jpg` — Bot Configuration → TRACK RECORD tab (hit rate / pending / rejected-that-would-have-won, calibration chart placeholder, per-decision rows with PENDING badges).
- `docs/screenshots/addons/chart_patterns_desktop.jpg` · `chart_patterns_390.jpg` — Dashboard → Market Posture → CHART PATTERNS row (OBSERVE ONLY badge once bars exist; "insufficient intraday bars" empty state shown).

## Changed files
New
- `backend/prediction_track.py` — outcome resolution (tp/sl/expired/no_data/pending, HORIZON_BARS 96, BAR_SEC 900, drift_r), stored-outcome reuse, Wilson z=1.96, calibration buckets.
- `backend/chart_patterns.py` — swings, zones (ZONE_TOL_ATR 0.5, ≥2 touches), trendlines (LINE_TOL_ATR 0.25, MIN_LINE_TOUCHES 3, PIVOTS_FOR_LINES 8), double top/bottom, H&S/inverse, triangles/wedges, candlesticks, no-look-ahead `measure_odds` (warmup 30, HORIZON 24, MIN_SAMPLES 12, one-sided Wilson z=1.64), `pattern_gate` (NEAR_ZONE_ATR 0.3, FRESH_BARS 8, min_lower_bound 0.5), `pattern_snapshot` (MIN_BARS 60, fail-safe `ready:false`).
- `backend/tests/unit/test_addons_spec.py` — 24 spec tests.
- `frontend/src/components/TrackRecord.jsx` — Track record tab.
- `docs/ADDONS_BUILD_SPEC.md`, `docs/screenshots/addons/` (4 images).

Modified
- `backend/routes/trade_routes.py` — `GET /api/trades/predictions` (symbol validation → 422, user-scoped, scan_limit 500).
- `backend/routes/posture_routes.py` — per-symbol `chart_patterns` (snapshot + would_veto BUY/SELL + enforced flag).
- `backend/bot_runner.py` — observe-only pattern layer after the structure gate; skips only when `pattern_gate_enabled` is true; signal carries `chart_patterns.would_veto`; fail-open.
- `backend/models.py` — `pattern_gate_enabled: bool = False`.
- `backend/routes/bot_routes.py` — `pattern_gate_enabled` admin-only + audited; serialized in config.
- `backend/rules_policy.py` — C1 (dict required, per-file try/except, log+skip), C2 (`issued_at > now+5min` → "issued_at is in the future").
- `backend/account_tombstones.py` — C3 (`token_hashes` index, BSON `expires_at` + TTL index `expireAfterSeconds=0`, ISO string kept for display).
- `backend/server.py` — tombstone index creation at startup.
- `backend/tests/unit/fake_mongo.py` — `create_index` kwargs / index listing for C3 test.
- `frontend/src/components/MarketPosture.jsx` — CHART PATTERNS AgentRow (`posture-patterns-{sym}`, mode badge, would-block line).
- `frontend/src/pages/BotConfig.jsx` — CONFIG / TRACK RECORD tab switch.
- `docs/TEST_MANIFEST.md`, `release/rc_lock.json`, `docs/RELEASE_SUMMARY.md` — regenerated via `scripts/generate_test_manifest.py` → `scripts/freeze_rc_lock.py` → `scripts/generate_release_summary.py`.
