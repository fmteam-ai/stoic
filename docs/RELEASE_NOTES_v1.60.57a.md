# STOIC v1.60.57a — #55 live-feature engines (flag, shadow compare) + #56 cross-account loss cooldown

Small, review-first PR ahead of the gold_trend_pulse PR (v1.60.57b). Protected paths untouched (EX5, policies/*,
release/ea_release.json, RELEASE_HASHES ea entry). Existing engine parameters are byte-identical (`DEFAULT_PARAMS`
asserted by test).

## New config fields (Bot Config, per account)
| field | default | effect |
|---|---|---|
| `engine_live_features_enabled` | `false` | #55 — hf_scalp / hf_scalp_fast / breakout_m15 trade on LIVE-price features; OFF = both engine outputs recorded side by side (`engine_shadow_compare`) |

No new environment variables. **With the default (`false`) the engines trade exactly as before** — the only always-on
change is #56, which BLOCKS re-entries (never opens / sizes / re-targets). New shared helper `backend/bars_util.py`
(resample / MT5 CSV loader / synthetic generator — pure functions).

## #56 — cross-account loss cooldown (always on; uses the existing `loss_cooldown_*` settings)
16 Oct: RoboForex 600007817961 XAUUSD BUY hit SL at 15:14:55 (−1R); `user_direction_cap` released the slot and OnEquity
opened the same XAUUSD BUY 43 s later (46732353, 15:15:38) — the loss cooldown was per account (iter-122b).
`loss_cooldown.loss_cooldown_block` now scans every account of the user: a bot close at a REAL loss (< −0.1R;
`r_multiple`, else derived from entry / stop / exit; R unknown → any negative P&L) on ANY account pauses that
symbol+direction (base symbol, so `XAUUSD.crp` ↔ `XAUUSD+`) on ALL accounts for the evaluating account's
`loss_cooldown_minutes` (default 30). Scratch / break-even closes of OTHER accounts (≥ −0.1R) do not trigger it; the
own-account rule is unchanged (any negative bot P&L). Manual trades and `stats_excluded` late fills never count.
Pulse "Cross-account loss cooldown: … paused on ALL accounts for N more min", counter
`cross_account_loss_cooldown_block`. Switching `loss_cooldown_enabled` off on an account disables both rules for it.
Tests: `tests/unit/test_v1_60_57_cross_account_loss_cooldown.py` (4).

## #55 — engines blind to fast gold moves (flag `engine_live_features_enabled`, default OFF)
1. `compute_intraday_features(bars, live_price, m5_bars)`: last_price / vwap_dist / donchian / range_pos use the LIVE
   broker price; EMAs and ATR stay on closed bars. New fields: `closed_last_price`, `feature_price_gap`,
   `stale_feature` (gap > 0.5×ATR15), `trend_slow`, `ema20_slope_pct_1h`, `m5_closes_rising/falling`, `donchian_hi20/lo20`.
2. breakout_m15: with live features the live price must still be ≥ 0.2×ATR15 beyond the broken level; a BREAK_DOWN
   whose live price is back inside is never sold (mirror for BUY).
3. hf_scalp / hf_scalp_fast momentum override: FLAT but live price ≥ VWAP + 0.5×ATR15, mom_3h ≥ mom_min, last 3 M5
   closes rising, structure ≠ LH_LL → treated as UP (BUY pullback to EMA20 or micro-breakout continuation); mirror DOWN.
4. Trend = slow stack OR fast confirmation (EMA20 slope over 1 h ≥ 0.05 % with live price on the right side of EMA20).
5. Anti-tilt: a loss on a `stale_feature_entry` (live price > 0.5×ATR15 from the closed-bar features at entry) no
   longer counts toward the streak (`streak_losses.classify_loss`).
6. Shadow compare: whichever pack the flag selects is traded; the OTHER pack is evaluated too and both outputs land on
   the signal/trade as `engine_shadow_compare` (actions, notes, trends, price gap, `differs`). Review one week before
   switching the flag ON. `scripts/engine_live_features_replay.py --csv … --from 2026-10-16T13:00 --to 2026-10-16T14:30`
   prints old vs new signals and 30-day win rate / PF after costs.


### One-week shadow comparison (do this BEFORE switching the flag ON)
Deploy with the flag OFF. Every engine decision (HOLD or signal) carries `engine_shadow_compare = {live_features_traded,
traded: {action, note}, other: {action, note}, feature_price_gap, stale_feature, trend_live, trend_closed, differs}` on
the signal / trade document; after a week compare how the `other` (live-feature) arm would have acted on the rows where
`differs` is true (`scripts/engine_live_features_replay.py --csv XAUUSD_M1.csv --days 30` gives the same old-vs-new table
from an MT5 M1 export, read-only). Only then set `engine_live_features_enabled=true` per account.

## Tests
`tests/unit/test_v1_60_57_engine_live_features.py` (7: live-price features vs closed, fast slope, momentum override and
its guards, breakout live-price confirmation / no stale SELL, stale-feature anti-tilt exemption, flag-OFF identity,
replay old-vs-new) and `tests/unit/test_v1_60_57_cross_account_loss_cooldown.py` (4).
