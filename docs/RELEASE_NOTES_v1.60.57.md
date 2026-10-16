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
| `gold_trend_pulse_sweep_runner_enabled` | `true` | item 13 — ½-size runner leg to the pre-drop high after a sweep & reclaim (inside the OFF engine) |
| `gold_trend_pulse_news_mode_enabled` | `true` | item 14 — PRE protect / RELEASE block / POST surprise-bias trades (inside the OFF engine) |
| `gold_trend_pulse_news_pre_action` | `"protect"` | `protect` (BE if ≥ +0.5R, else keep SL) \| `close` \| `tighten` (SL → −0.5R) |
| `gold_trend_pulse_news_release_min` | `5` | RELEASE window T → T+N min (1–30) with no entries |
| `gold_trend_pulse_news_events` | `null` | event kinds; null = CPI, Core CPI, Nonfarm Payrolls, FOMC, Fed Interest Rate Decision, Fed Chair, PCE, GDP, Retail Sales, PPI, ISM |
| `engine_live_features_enabled` | `false` | #55 — hf_scalp / hf_scalp_fast / breakout_m15 trade on LIVE-price features; OFF = both outputs recorded (`engine_shadow_compare`) |

No new environment variables. **v1.60.56 added no flags at all** (its table says "—"); v1.60.57's complete list is the
table above — nothing else is switchable.

### Default behaviour: unchanged on every account
With the defaults (`gold_trend_pulse_enabled=false`, `engine_live_features_enabled=false`) **nothing in v1.60.56 / v1.60.57
changes trading on any account**: `ai_signals` never enters the gold_trend_pulse branch (`enabled_for(cfg)` is false, so
XAUUSD is analysed by the preset engine exactly as before, no `gtp_*` collections are written, no XAUUSD is added to the
symbol list, the generic cooldown / trade-of-day cap stay in force), the engines read closed-bar features exactly as
before (#55 only RECORDS the live-feature arm side by side as `engine_shadow_compare`), `DEFAULT_PARAMS` are
byte-identical, the news-mode / sweep / layering sub-flags live INSIDE the disabled engine and therefore do nothing. The
only runtime changes that are always on are #56 (cross-account loss cooldown — blocks, never opens) and the v1.60.56 safety follow-ups (#49–#54: close-alert dedup, close-reason
priority, netting-ack exclusion, more mandatory close reasons, real entry costs in the break-even buffer, optimizer may
not switch `breakeven_enabled` off, sector-pulse dedup) — none of them opens, sizes or re-targets a position.
Asserted by `test_release_notes_flag_table_matches_model_defaults_and_engine_is_inert_when_off` (the flag table above is
parsed and compared with `BotConfigUpdate` defaults) and `test_engine_registered_as_deterministic_scope_and_off_by_default`.

### Running the replays on the VPS (inside the backend image, READ-ONLY)
```
docker compose cp XAUUSD_M1.csv backend:/tmp/XAUUSD_M1.csv                      # MT5 export: <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>, broker UTC+3
docker compose exec backend python scripts/gtp_replay.py --csv /tmp/XAUUSD_M1.csv --tz-offset 3 --days 30 --spread 0.25 --commission <acct> --out /tmp/gtp_30d.md --trades-csv /tmp/gtp_30d.csv
docker compose exec backend python scripts/gtp_replay.py --csv /tmp/XAUUSD_M1.csv --tz-offset 3 --days 0 --window 2026-10-16T13:15 2026-10-16T14:30 --out /tmp/gtp_16oct.md
docker compose exec backend python scripts/gtp_news_events.py --months 6 --out /tmp/news_events.json --csv-out /tmp/news_events.csv      # FINNHUB_API_KEY(_FILE) from the staged secret
docker compose exec backend python scripts/gtp_replay.py --csv /tmp/XAUUSD_M1.csv --tz-offset 3 --days 186 --news-events /tmp/news_events.json --out /tmp/gtp_news_6m.md
docker compose exec backend python scripts/engine_live_features_replay.py --csv /tmp/XAUUSD_M1.csv --from 2026-10-16T13:00 --to 2026-10-16T14:30
docker compose cp backend:/tmp/gtp_30d.md . && …                                 # markdown / CSV out only — no DB writes, no orders
```

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

## Item 13 — sweep & reclaim entry (bias direction only)
* **Setup** (UP bias; DOWN mirrored): a fast drop ≥ 1.5×ATR14(M5) within ≤ 6 M5 bars that trades BELOW the lowest low
  of the 2 h (24 M5 bars) before the drop; in the same or the next 3 bars an M5 bar CLOSES back above that swept level
  with a body ≥ 50 % of its range → BUY at the close. Tried BEFORE the pullback / micro-breakout on every M5 close.
  Never the opposite trade; breakout_m15 never sells a BREAK_DOWN whose live price is back inside the channel (#55-2).
* **Exits**: SL = sweep low − max(0.30, 0.25×ATR14); scalp TP = clamp(0.7×ATR14, 2.00, 4.50); optional ½-size runner to
  the pre-drop high (only when it lies beyond the scalp TP) whose stop goes to breakeven+costs once the scalp TP is
  hit (`be_trigger_price`; 90-min time-stop). Both legs carry broker-side SL and TP (two tickets, same `setup_id` =
  `basket_id` → ONE slot for `user_direction_cap`; the runner ticket is emitted on the next pass only if the scalp leg
  opened within 5 min). Total risk stays 0.25 % (lots split; no runner when a leg would fall under 0.01). The SL of a
  sweep is structurally wider than the scalp TP, so the 1.3×TP rule is applied against the setup's target (runner
  target when present) with a floor of 3.5×ATR14 — reclaims that already ran too far are skipped.
* All filters / limits / guards unchanged; no entry within ±15 min of USD high-impact news (and the news-mode PRE /
  RELEASE phases block it too).
* **Replay** marks EVERY sweep & reclaim (both directions, `sweeps.marked`), how many had the bias and how many the
  rule traded (`caught`), scalp / runner win rate, avg win/loss; `--window 2026-10-16T13:15 2026-10-16T14:30` prints a
  per-M5-close trace for the 16 Oct question (expected: BUY near 4160–4165 — needs the real M1 export).

## Item 14 — news mode (high-impact USD events)
* **Source**: Finnhub economic calendar (actual / forecast / previous) through `economic_calendar.get_events()` — legacy
  feeds as fallback. Event kinds configurable (`gold_trend_pulse_news_events`).
* **PRE** (T−15 → T): no new entries. Open GTP positions: ≥ +0.5R → stop to breakeven+costs; else per policy
  (`protect` keeps the stop, `close` closes, `tighten` moves it to −0.5R). Live rows via the fenced MODIFY_SL /
  FULL_CLOSE paths (`gtp_news_pre_*`, close_watchdog applies); shadow rows adjusted in place; every action logged on
  the event row (`gtp_news_events.pre_actions`).
* **RELEASE** (T → T+`release_min`): no entries; the spread filter must pass before any later entry.
* **POST** (T+release → T+90): surprise = actual − forecast (raw units; per-event thresholds CPI/Core CPI/PCE/PPI 0.1 pp,
  NFP 25k, GDP 0.3, Retail Sales 0.2, ISM 1.0): beats → USD strong → XAU DOWN, misses → UP, below threshold → NONE
  (normal daily bias applies). FOMC / rate decision / Fed Chair: direction of the first 15-min candle. Confirmation:
  the first M5 close outside the 30-min pre-release range within 15 min must agree — otherwise NO news trade for that
  event. Entry: first pullback to EMA20(M5) or the 38–50 % retrace of the initial move in the bias direction, or a
  sweep & reclaim (item 13). TP = clamp(0.7×ATR14 post-release, 3.00, 8.00); SL beyond the pullback / sweep extreme
  (+0.30), max 1.3×TP; 0.25 % risk; 60-min time-stop; broker-side SL/TP; max 2 news trades per event per account;
  daily limits unchanged. The news trade ignores the engine's own ±15-min blackout for THAT event only — the main
  pipeline's macro freeze / avalanche still gate a demo_live order.
* **Reporting**: per event (`gtp_news_events`): surprise, bias, first-move direction, confirmed, trades taken /
  skipped and why, PRE actions; Scalp card news block; Market Intel shows the next event and its plan ("CPI 12:30 UTC —
  protect at 12:15, news trades from 12:35"); Strategy Report `gold_trend_pulse.news_events` + `NEWS` regime stats.
* **Replay**: `scripts/gtp_news_events.py --months 6` exports the Finnhub events; `scripts/gtp_replay.py --csv …
  --days 180 --news-events news_events.json` reports NEWS trades separately (win rate, avg win/loss, worst trade, by
  kind, per-event table) — normal-session stats exclude them.

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

## Review items #62–#70 (fixed before shadow enable)
* **#62** `bot_runner`: trend-ride TP widening (×1.8), and therefore any TP rescale, is skipped for every deterministic
  scope (`gold_trend_pulse`, hf_scalp, hf_scalp_fast, range_fade, breakout_m15) — `trend_ride_skipped` says so; GTP exits
  in trade_manager already return before vol-retarget / tiers.
* **#63** Spread is resolved via `entry_costs.spread_at_entry` (broker symbol GOLD# / XAUUSD+ / XAUUSD.crp / XAUUSD →
  base symbol); missing spread or `spreads_updated_at` older than 120 s → **fail closed** (no entry, note / pulse
  "spread unknown … entry refused" / "spread stale").
* **#64** `gold_trend_pulse_mode` is a validated enum (`shadow` | `demo_live`); the generic Bot Config PATCH refuses
  `demo_live` (422 `gtp_confirm_live_required`) — only `POST /api/gold-trend-pulse/config` with `confirm_live=true` on an
  account whose server-side attestation is DEMO (`broker_env.attested_environment`) may set it; `effective_mode` falls back
  to shadow on a non-DEMO account. Live gate = rolling 30-day window of shadow **and** live outcomes (re-evaluated every
  pass, never frozen); a basket (layered legs / sweep scalp+runner) counts once (`collapse_baskets`).
* **#65** Layered legs: the shared stop must lie beyond the leg's entry, SL ≤ 1.3×TP per leg, and every leg needs the
  same RSI-reset / M5-close confirmation as L1 (`detect_entry`); skipped legs are recorded on the basket.
* **#66** Management ≠ entry: during a macro freeze `ai_signals` runs a management-only GTP pass (news PRE protection,
  basket invalidation, range-breakout closes; no entries). `_news_protect` honours the broker min stop distance
  (`be_clamped_skip`) and marks a live trade "protected" only after the EA cleared the MODIFY_SL (`gtp_news_protect_pending`
  → `gtp_news_protected`).
* **#67** An M5 bar is evaluated only when its window elapsed AND all 5 M1 bars are present (`complete_m5`; `last_m5_t`
  never advances on an incomplete bar); bias uses completed H1/H4 only; H4 is read from the EA H4 stream (fallback
  resample) and needs ≥ 200 completed bars for EMA50 (else bias NONE "H4 warm-up"); `t_basis=broker_unknown` → no
  today-open / prior-day mid → bias NONE.
* **#68** Sweep: the scalp leg must satisfy SL ≤ 1.3×scalp TP (else skip); the runner keeps its own cap; total 0.25 %.
* **#69** Break-even / |net| ≤ costs exits are **scratch**, not loss (daily 3-loss stop, cooldown, live-gate PF/expectancy:
  `is_loss`, `consecutive_losses`); R always from `original_stop_loss`; live BE cost buffer uses the stored
  `spread_at_entry` + commission (#53) instead of the 2.5-pip default.
* **#70** Event matching is whole-word (no "ism" inside "optimism") and HIGH impact only; the 15/day cap counts opened
  trades; dead RANGE breakout line removed; `--tz-offset` applies to CSV bars only (DB bars are already UTC).
* Tests: `tests/unit/test_gold_trend_pulse_review_62_70.py` (9).

## Tests
`tests/unit/test_gold_trend_pulse.py` (20: bias UP/DOWN/NONE + hysteresis, both triggers, spike filter, TP/SL clamps
incl. skip, BE sign BUY/SELL with costs, time-stop, labeling, every filter/limit, sizing via pip value, live gate,
layering levels / shared stop / basket risk cap / direction-cap basket slot, range detection / entries / exits /
breakout, replay both modes + baskets, pipeline hooks, trade_manager exits, guard coverage),
`tests/unit/test_gold_trend_pulse_news_sweep.py` (14: sweep detection UP/DOWN mirror + never the opposite trade, exit
geometry / runner / lot split, runner BE in labeling, breakout_m15 reclaimed-level refusal, event matching / phases /
configurable list, surprise mapping + thresholds + FOMC candle, confirmation agree/disagree/pending, news entry +
clamps + time-stop + 2-per-event, PRE policies, engine PRE protect / RELEASE block / POST trade / cap / not-confirmed,
scalp+runner legs as one basket, defaults / routes / card fields, replay sweeps + news + window trace) and
`tests/unit/test_v1_60_57_engine_live_features.py` (7).
