# Known test failures (environment-only) — listed per main92 review A8a

Reviewer ask: "list the pre-existing failures". None of these is a product defect; each needs a
runtime the preview sandbox does not provide. CI (DB_NAME=stoic_ci / stoic_e2e, backend on the
same database, indexes built at boot) runs them green.

| Suite | Marker | Why it fails in the preview sandbox | How to run it properly |
|---|---|---|---|
| `tests/integration/scalp/test_scalp_api.py` (7 setup errors) | http | Registers a user through the running preview API (DB `ai_trading_bot`) but reads it back from `DB_NAME=ai_trading_bot_test` — the two databases differ, so `make_elite` finds no user | Run with the backend and the suite on the same DB (`STOIC_TESTS_ALLOW_DB_NAME=1 DB_NAME=ai_trading_bot`) or in CI |
| `tests/integration/test_iter160_review_p0.py::test_health_score_fail_closed_caps` | integration | Intermittent `OSError: connection closed` from the sandbox Mongo under parallel load | Re-run; CI uses a dedicated mongod |
| `tests/test_iter196_authority_truth.py` (6) / `tests/test_iter238_authority_round9.py` (3) | http | Expect the unique indexes (`broker_deals`, `scalp_financial_events`, ticket index) and authority state that the API creates at boot on ITS database; the test DB has no running API | Same-DB run as above |
| `tests/test_iter14_gold_edge.py::test_retrain_returns_global_plus_per_session` | unit | Needs ≥ N historical gold trades in the DB to produce per-session models | Seed the fixture set (`scripts/seed_test_data.py --gold`) |
| `tests/test_iter211_corrections.py::TestBolaMatrix::test_every_sensitive_route_declared` | http | Reads the live `/openapi.json`; the preview backend must be restarted on the current code first | Restart backend, re-run |

The standard unit lane (`DB_NAME=ai_trading_bot_test python -m pytest tests -m "not http and not broker and not external and not chaos and not soak"`) deselects every row above except the `integration/` ones; it reports **1029 passed, 1 skipped** plus the two environment rows.

## CI e2e (Playwright) — root cause of the main92 red run
Fix plan D2 made every admin read (`/ops/*`, `/authority/*`) require enrolled admin TOTP. The per-run CI admin
has no authenticator, so `bot health ops cards` found no cards (403). Reproduced locally with a production
build + fresh DB (fails with `ADMIN_MFA_ENFORCED=true`, 13/13 pass with the documented CI escape hatch
`ADMIN_MFA_ENFORCED=false`, which `server.py` refuses under `APP_ENV=production`). The env is now set on the
`frontend-e2e` job. Admin-MFA enforcement itself stays covered by the unit + integration lanes.
