# PR: v1.60.58 — review fixes #57–#61 for v1.60.56/57 (+ gold_trend_pulse pre-shadow fixes #62–#70)

Base: GitHub `main` @ `347f421` (v1.60.55). Replaces PR #107/#108 (NO-GO). Branch `release/v1.60.58` (via Save to
GitHub). No tag. No new flags; no default changes on any account; protected paths untouched.
Notes: `docs/RELEASE_NOTES_v1.60.58.md`, `docs/RELEASE_NOTES_v1.60.57.md` (GTP #62–#70 section).

## Review items
* **#57 BLOCKER** — `@router.post("/external-deal")` was bound to the `_stamp_entry_costs` helper → every EA
  external-deal POST would 422. Decorator back on `external_deal`. New tests: ASGI POST of a real
  `BridgeExternalDeal` through `server.app` (200 + trade updated) and a route table asserting the endpoint function
  name for all 9 `/bridge/*` routes.
* **#58 HIGH** — `stale_feature_entry` gated on `engine_live_features_enabled` (flag OFF ⇒ always False ⇒ the
  2-losses-in-4h guard is byte-identical to before). Test.
* **#59 MEDIUM** — `STREAK_FIELDS` projects `stale_feature_entry` (anti_tilt / learning_pipeline see it when ON).
* **#60 MEDIUM** — cross-account cooldown R from `original_stop_loss` (BE scratch ≠ real loss). Test with BE scratch.
* **#61 LOW** — quiet window never pauses panic/protection re-dispatch (`ea_quiet(..., reason)` in watchdog +
  `mandatory_close`); friday_flat close Telegram also fires after an earlier tighten.
* **#62–#70** (gold_trend_pulse, still OFF): trend-ride/vol-retarget exclusion for deterministic scopes, fail-closed
  spread by broker symbol (GOLD# / XAUUSD+ / XAUUSD.crp / XAUUSD), demo_live only on attested DEMO + enum + generic
  PATCH refuses `demo_live` (422) + rolling gate counting a basket once, layered-leg SL side / 1.3×TP / confirmation,
  management during macro freezes with ack-based "protected", complete-M5 / completed-H1-H4 / H4 warm-up / t_basis,
  sweep scalp SL cap, scratch outcomes + original-stop R + stored entry costs for BE, whole-word HIGH-impact event match,
  dead code removed, `--tz-offset` CSV-only.

## Files
`routes/bridge_routes.py`, `ai_signals.py`, `streak_losses.py`, `loss_cooldown.py`, `close_watchdog.py`,
`close_commands.py`, `friday_flat.py`, `bot_runner.py`, `trade_manager.py`, `models.py`, `routes/bot_routes.py`,
`routes/gold_trend_pulse_routes.py`, `gold_trend_pulse/*`, tests
(`test_v1_60_58_review_57_61.py` 10, `test_gold_trend_pulse_review_62_70.py` 9), docs.

## Validation
Unit lane green on this branch (see PR checks); `scripts/secret_scan.py` 0 blocking; `check_protected_paths.py` OK;
release artifacts regenerated (`TEST_MANIFEST.md` → `rc_lock.json` → `RELEASE_SUMMARY.md`). Real-data replays
(30 days XAUUSD M1, 16 Oct 13:15–14:30, 6-month CPI/NFP/FOMC) still to be run on the VPS before any GTP shadow enable.
