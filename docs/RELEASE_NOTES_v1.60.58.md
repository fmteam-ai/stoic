# v1.60.58 — review fixes for PR #107/#108 (v1.60.56/57 + #56): #57–#61

Supersedes the NO-GO on #107/#108. No new flags, no default change on any account; protected paths untouched
(EX5, `policies/*`, `release/ea_release.json`, EA entry in `RELEASE_HASHES`). Also carries the gold_trend_pulse
pre-shadow review fixes #62–#70 (see `docs/RELEASE_NOTES_v1.60.57.md`, engine still `gold_trend_pulse_enabled=false`).

| item | severity | fix |
|---|---|---|
| **#57** | BLOCKER | `routes/bridge_routes.py`: the `@router.post("/external-deal")` decorator had been left on the `_stamp_entry_costs` helper, so FastAPI registered the helper (query params `db`, `payload` + body `trade`, `acc`) as the endpoint and every EA `external-deal` POST would 422. Decorator moved back onto `external_deal`. Tests: ASGI integration test (`httpx.ASGITransport(app=server.app)` → POST a real `BridgeExternalDeal` → 200 + `commission_entry` stamped on the trade, `broker_deals` row written) and a route-table test asserting the endpoint function name of **every** `/bridge/*` route (no `_private` helper may be bound). |
| **#58** | HIGH | `ai_signals.py`: `stale_feature_entry` is now `bool(_live_on and _pack_live.stale_feature)` — with `engine_live_features_enabled=false` it is always `False`, so `streak_losses.classify_loss` counts those losses exactly as before (bot_runner 2-losses-in-4h same-direction guard unchanged with the flag OFF). |
| **#59** | MEDIUM | `streak_losses.STREAK_FIELDS` projects `stale_feature_entry`, so `anti_tilt.evaluate` and `learning_pipeline` actually see the field (effective only when the flag is ON, by #58). |
| **#60** | MEDIUM | `loss_cooldown.trade_r_multiple` derives R from `original_stop_loss` (falls back to `stop_loss`; the non-existent `initial_stop_loss` is gone) — a BE-moved stop sitting at entry no longer turns a few-cent scratch into a "real loss" that freezes the symbol+direction on all accounts. Test with a BE scratch (−0.3 $ → R ≈ −0.0075 → not a loss) and a real −1R. |
| **#61** | LOW | `close_watchdog.ea_quiet(account, now, reason)`: panic / emergency / protection / pre_news_protect / unprotected_position / late_fill / intentional_close / hard_drawdown (reason rank ≤ 1) are **never** paused by the EOD quiet window (23:40–00:05 broker time, which with an unknown offset = 0 lands on 23:40–00:05 UTC); `close_commands.mandatory_close` and the watchdog sweep pass the reason. friday_flat / eod_flatten still defer. `friday_flat`: the close Telegram fires when the CLOSE action first touches a trade even if `friday_flat_at` was set by an earlier tighten (`friday_flat_action != "close"`); re-issues stay silent. |

Tests: `tests/unit/test_v1_60_58_review_57_61.py` (10). Full unit lane green (see PR checks).

## Default behaviour: unchanged on every account
No flag added or changed. `engine_live_features_enabled=false`, `gold_trend_pulse_enabled=false`,
`gold_trend_pulse_mode=shadow` as before (full table in `docs/RELEASE_NOTES_v1.60.57.md`). #57 restores the
pre-#53 EA deal path; #58/#59 restore byte-identical anti-tilt behaviour with the live-feature flag OFF; #60 only
narrows the cross-account block; #61 only removes a pause from risk-reducing closes.
