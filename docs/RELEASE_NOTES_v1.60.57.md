# STOIC v1.60.57 — gold_trend_pulse (NEW engine, shadow-first) + #55 live-feature engines (shadow compare)

Protected paths untouched (EX5, policies/*, release/ea_release.json, RELEASE_HASHES ea entry). **No new trading effect
unless a flag is switched ON per account.** Existing engines (hf_scalp, hf_scalp_fast, breakout_m15, balanced …) and
their parameters are unchanged (`DEFAULT_PARAMS` identical, verified by test).

## New config fields (Bot Config, per account)
| field | default | effect |
|---|---|---|
| `gold_trend_pulse_enabled` | `false` | enables the XAUUSD-only engine; XAUUSD is evaluated even if not in the preset symbol list |
| `gold_trend_pulse_mode` | `"shadow"` | `shadow` \| `demo_live`; demo_live runs SHADOW until the engine's OWN live gate passes (card says why) |
| `gold_trend_pulse_layering_enabled` | `false` | item 11 — up to 3 staggered legs per pullback, one shared stop, basket risk ≤ 0.75 % |
| `engine_live_features_enabled` | `false` | #55 — hf_scalp / hf_scalp_fast / breakout_m15 trade on LIVE-price features; OFF = both outputs recorded (`engine_shadow_compare`) |

No new environment variables.

## gold_trend_pulse — how it trades (all distances in PRICE units; 1 retail pip = 0.10)
* **Daily bias** every 15 min (hysteresis: 2 consecutive confirmations): UP = H4 close > EMA50(H4) rising, H1 close >
  EMA50(H1) rising, price > today's 00:00 UTC open, price > prior-day mid; DOWN mirror; else NONE. Bias + 4 reasons on
  the Scalp card and the Market Intel card.
* **Regime** every M5 close: TREND (bias UP/DOWN) · RANGE (bias NONE + confirmed M5 range: ≥2 touches of each band over
  2 h, width 2.5–8×ATR14, no M15 close outside in 30 min) · NO_TRADE.
* **TREND entries** on the last COMPLETED M5 bar, bias direction only: (a) pullback to EMA20(M5)/session VWAP (±0.30),
  RSI(7) dipped <40 (>60) and turned, M5 closed with the trend; (b) micro-breakout of the last 3 M5 highs/lows after
  such a pullback. Spike filter: last M5 range > 2.5×ATR14 → no entry. Trigger recorded on the decision.
* **TREND exits**: TP = clamp(0.7×ATR14, 2.00, 4.50); SL = swing extreme + 0.30, max 1.3×TP (else skip, pulse "SL too
  wide for target"); break-even at 60 % of TP → entry ± (spread + commission); 45-min time-stop; single TP on all
  legs — no TP rescale / stretch / vol-retarget / trend-ride (trade_manager returns before those paths).
* **RANGE entries** on the last completed M1 bar: SELL on a rejection from the top 15 % (upper wick ≥ 50 %, close back
  inside), BUY mirror; TP = min(range mid, clamp(0.5×ATR14, 1.00, 3.00)); SL beyond the edge + max(0.30, 0.25×ATR14),
  max 1.3×TP; BE at 60 %; 20-min time-stop. M5 close outside the range → no new RANGE trades, open RANGE trades on
  the wrong side are closed (`gtp_range_breakout`, fenced FULL_CLOSE), TREND takes over once the bias confirms.
* **Layering** (flag): L1 EMA20(M5) touch, L2 session VWAP, L3 = L1 − 1.0×ATR14 (mirror DOWN); each leg its own TP;
  ONE shared stop = day's last M15 swing extreme + 0.30 on every leg; legs sized at 0.25 % to the shared stop, skipped
  when the basket would exceed 0.75 %; bias flip / M15 close beyond the stop structure → no new legs, basket closed
  (`gtp_basket_invalidated`); a basket is ONE position for `user_direction_cap` (same account only); max 1 basket per
  account; max 15 legs/day. Every leg carries a broker-side SL.
* **Filters & limits**: spread ≤ 25 % of TP (live broker spread, `spread_at_entry` recorded); one open position per
  account; cooldown 3 min after a win / 15 min after a loss; daily stop after 3 consecutive losses or −1.5 % equity;
  max 15 trades/day; sessions 07:00–20:00 UTC Mon–Thu, 07:00–18:00 Fri; no entries ±15 min around USD high-impact
  events (economic calendar) — existing macro freeze / avalanche still apply before the engine runs.
* **Sizing**: fixed 0.25 % of equity from the SL distance via the authoritative pip value (`pip_value_usd_per_lot`,
  #40/#42); min lot 0.01, `max_lot_size` respected; planned risk $ and % on every decision; the main pipeline's
  pip-value anomaly / raw-lot guards and `risk_pct_cap` (0.25) run again before the order.
* **Live gate (own)**: ≥ 100 labeled shadow trades AND expectancy > 0 after spread + commission AND PF > 1.2 AND max
  drawdown < 5R. Shadow labels walk the user's broker M1 bars (EA M1 feed, else M1 built from the scalp tick store with
  the #34 broker offset) with full costs; SL-first when both touched in a bar.
* **Guards**: a demo_live decision becomes an ordinary deterministic main-pipeline signal (`scope=gold_trend_pulse`)
  — authority/CLOSE_ONLY, panic, Friday flat, EOD flatten, anti-tilt (shared helper), daily loss limits, macro
  freeze/avalanche, Asia/gold off-hours windows, user_direction_cap, pip-value/raw-lot guards, close_watchdog,
  broker-price management and stale-quote refusal all apply unchanged. Only the generic per-symbol signal cooldown
  and trade-of-day cap are replaced by the engine's own 3/15-min cooldown and 15/day cap (XAUUSD, flag ON only).
* **Reporting**: `GET /api/gold-trend-pulse/card?account_id=` (bias + reasons, regime, basket, today / 7-day trades,
  win rate, avg win/loss $, PF, expectancy after costs, gate n/100, last 20 decisions with trigger / entry / SL / TP /
  spread / risk / outcome / R / MFE-MAE / time / exit); `POST /api/gold-trend-pulse/config`; Market Intel
  `gold_trend_pulse` section; Strategy Report `gold_trend_pulse` (shadow by regime/trigger, live, skipped).
* **Replay**: `scripts/gtp_replay.py --csv <MT5 M1 export> [--layering]` → trades/day, win rate, PF, expectancy after
  costs, max DD, by session/hour/trigger/exit, TREND vs RANGE, regime minutes per day, basket stats.

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

## Tests
`tests/unit/test_gold_trend_pulse.py` (20: bias UP/DOWN/NONE + hysteresis, both triggers, spike filter, TP/SL clamps
incl. skip, BE sign BUY/SELL with costs, time-stop, labeling, every filter/limit, sizing via pip value, live gate,
layering levels / shared stop / basket risk cap / direction-cap basket slot, range detection / entries / exits /
breakout, replay both modes + baskets, pipeline hooks, trade_manager exits, guard coverage) and
`tests/unit/test_v1_60_57_engine_live_features.py` (7).
