# Known test failures (environment-only) — listed per main92 review A8a

Reviewer ask: "list the pre-existing failures". None of these is a product defect; each needs a
runtime the preview sandbox does not provide. CI (DB_NAME=stoic_ci / stoic_e2e, backend on the
same database, indexes built at boot) runs them green.

| Suite | Marker | Why it fails in the preview sandbox | How to run it properly |
|---|---|---|---|
| `tests/integration/scalp/test_scalp_api.py` (7 setup errors) | http | Registers a user through the running preview API (DB `ai_trading_bot`) but reads it back from `DB_NAME=ai_trading_bot_test` — the two databases differ, so `make_elite` finds no user | Run with the backend and the suite on the same DB (`STOIC_TESTS_ALLOW_DB_NAME=1 DB_NAME=ai_trading_bot`) or in CI |
| `tests/integration/test_iter160_review_p0.py::test_health_score_fail_closed_caps` | integration | Intermittent `OSError: connection closed` from the sandbox Mongo under parallel load | Re-run; CI uses a dedicated mongod |
| `tests/test_iter196_authority_truth.py` (6) / `tests/test_iter238_authority_round9.py` (3) | http | Expect the unique indexes (`broker_deals`, `scalp_financial_events`, ticket index) and authority state that the API creates at boot on ITS database; the test DB has no running API | Same-DB run as above |

Corrections after review (2026-10-05):
- `tests/test_iter14_gold_edge.py::test_retrain_returns_global_plus_per_session` is **not** a failure: since the C2
  commit it `pytest.skip`s when the DB holds < 30 closed gold trades ("retrain not trained: need ≥30 closed trades").
  It passes on a seeded DB. The earlier "environment failure" entry was wrong.
- `tests/test_iter211_corrections.py::TestBolaMatrix::test_every_sensitive_route_declared` builds the OpenAPI spec
  in-process from `server.app` and was a **real** gap (14 sensitive routes undeclared, incl. the N11 admin
  position-mode / environment routes). All 14 are now declared in `security_matrix.BOLA_MATRIX` with their actual
  enforcement (`docs/BOLA_MATRIX.md` regenerated); the test passes.

The standard unit lane (`DB_NAME=ai_trading_bot_test python -m pytest tests -m "not http and not broker and not external and not chaos and not soak"`) deselects every row above except the `integration/` ones; it reports **1029 passed, 1 skipped** plus the two environment rows.

## CI e2e (Playwright) — root cause of the main92 red run (now tested WITH admin MFA on)
Fix plan D2 made every admin read (`/ops/*`, `/authority/*`) require enrolled admin TOTP. The per-run CI admin
has no authenticator, so `bot health ops cards` found no cards (403). Reproduced locally with a production
build + fresh DB. The `ADMIN_MFA_ENFORCED=false` stopgap was replaced the same day: the job now runs
`scripts/ci_enrol_admin_totp.py` (generated base32 secret written on the per-run admin, masked, exported as
`E2E_ADMIN_TOTP_SECRET`) and `e2e/tests/auth.setup.ts` computes the RFC 6238 code (`e2e/tests/totp.ts`) when the
login asks for it — 13/13 pass with `ADMIN_MFA_ENFORCED=true` (verified locally against the production build).

## main93 / A9 note (2026-10-05)
- `tests/test_main93_live_iter229.py` (http lane, written by the testing agent) reads `TEST_ADMIN_PASSWORD` and
  `STEP_UP_BYPASS_TOKEN` from the environment and SKIPS at module level when they are absent — never embed them.
- `tests/integration/test_r26_device_attestation.py::test_concurrent_submissions_of_one_nonce_admit_exactly_one`
  flaked once in a full-lane run (timing), passes alone and on re-run — not a regression.
