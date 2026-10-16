# PR: v1.60.57b — gold_trend_pulse (NEW XAUUSD engine, SHADOW first)

**Sequencing:** #55 + #56 ship first as the small PR **v1.60.57a** (branch `release/v1.60.57a-55-56`, patch
`release/patches/v1.60.57a-55-56.patch`, text `docs/PR_v1.60.57a.md`) for the Sunday deploy; this PR is the remainder (gold_trend_pulse
items 1–14) and applies on top of it.

Base: GitHub `main` @ `347f421` (v1.60.55). **Sync note:** this sandbox has no `origin` remote / token, so the branch
was built on the local tree (v1.60.55 + v1.60.56 follow-ups) — open the PR with *Save to GitHub* onto `main@347f421`
and resolve as needed. Protected paths untouched. Not tagged. Full notes: `docs/RELEASE_NOTES_v1.60.57.md`.

## Versions and their items
| version | items | notes |
|---|---|---|
| **v1.60.56** | #49 close/Telegram alert dedup + quiet window · #50 mandatory close-reason priority (panic > protection > friday_flat > eod > manual) · #51 `netting_close_acked` legs excluded from re-dispatch / fan-out · #52 more mandatory reasons, discretionary not expired within 120 s of an ack · #53 real entry costs (`entry_costs.py`) in the BE buffer, `breakeven_enabled` out of the optimizer · #54 sector-pulse dedup | `docs/RELEASE_NOTES_v1.60.56.md` — **no new flags**; always-on safety follow-ups, none opens / sizes / re-targets a position |
| **v1.60.57** | gold_trend_pulse items 1–10 (engine, shadow first) · 11 layering · 12 range mode · 13 sweep & reclaim (+ runner) · 14 news mode · **#55** live-price features / momentum override / stale-breakout fix (`engine_live_features_enabled`, OFF, shadow compare) · **#56** cross-account loss cooldown (always on, blocks only) | `docs/RELEASE_NOTES_v1.60.57.md` — flag table below; **#55 is in v1.60.57, not v1.60.56** |

## What is new (all OFF by default — no trading effect until a flag is switched ON per account)
| flag | default | what it switches on |
|---|---|---|
| `gold_trend_pulse_enabled` | false | XAUUSD-only M5 trend-pullback scalper (items 1–10), auto RANGE mode (item 12) |
| `gold_trend_pulse_mode` | shadow | `demo_live` runs SHADOW until the engine's own gate passes (≥100 labeled, expectancy>0 after costs, PF>1.2, DD<5R) |
| `gold_trend_pulse_layering_enabled` | false | item 11 — 3-leg baskets, one shared stop, ≤0.75 % basket risk, basket = one direction-cap slot |
| `gold_trend_pulse_sweep_runner_enabled` | true (inside the OFF engine) | item 13 — ½-size runner leg to the pre-drop high after a sweep & reclaim |
| `gold_trend_pulse_news_mode_enabled` / `_news_pre_action` / `_news_release_min` / `_news_events` | true / protect / 5 / default list (inside the OFF engine) | item 14 — PRE protect, RELEASE block, POST surprise-bias trades |
| `engine_live_features_enabled` | false | #55 — engines trade on LIVE-price features; OFF records old-vs-new side by side (`engine_shadow_compare`) |

Existing engines and their parameters are byte-identical (`DEFAULT_PARAMS` asserted by test). A gold_trend_pulse live
decision is an ordinary deterministic main-pipeline signal, so every guard (authority/CLOSE_ONLY, panic, Friday flat,
EOD flatten, anti-tilt, daily loss, macro freeze/avalanche, gold off-hours, user_direction_cap, pip-value/raw-lot,
close_watchdog, broker-price management, stale-quote refusal) applies unchanged — `test_every_existing_guard_still_applies_to_gold_trend_pulse`.

## Validation (item 8 / #55-6) — what could and could not be run here
**No real XAUUSD M1 exists in this sandbox** (no gold tick batches, no EA M1 feed; one stale M15 document from Aug 2026),
so the 30-day gold replay and the 16 Oct 13:00–14:30 window **must be run on production or on an MT5 History export**:

```
cd backend
python scripts/gtp_replay.py --csv XAUUSD_M1.csv --days 30 --spread 0.25 --commission <per account>      # (+ --layering)
python scripts/engine_live_features_replay.py --csv XAUUSD_M1.csv --from 2026-10-16T13:00 --to 2026-10-16T14:30
python scripts/engine_live_features_replay.py --csv XAUUSD_M1.csv --days 30
python scripts/gtp_replay.py --csv XAUUSD_M1.csv --days 30 --window 2026-10-16T13:15 2026-10-16T14:30     # item 13: sweeps marked/caught + 16 Oct trace
FINNHUB_API_KEY=… python scripts/gtp_news_events.py --months 6 && python scripts/gtp_replay.py --csv XAUUSD_M1.csv --days 180 --news-events news_events.json   # item 14
```
All scripts are read-only (markdown / CSV out, no DB writes, no orders) and run inside the deployed backend image —
exact VPS commands in the release notes ("Running the replays on the VPS"). `--tz-offset 3` converts the MT5 export
(broker UTC+3) to UTC; `gtp_news_events.py` pulls the Finnhub history in 31-day chunks with the staged key. Paste the
outputs into this PR before GO.

### Mechanics smoke on SYNTHETIC M1 (random-walk generator, `gold_trend_pulse/synthetic.py`) — NOT a performance claim
gold_trend_pulse, 30 synthetic days, spread 0.25, commission $3.5/lot/side, equity $10,000:

| metric | value |
|---|---|
| trades | 13 (0.59/day) · TREND 9 · RANGE 4 |
| win rate / PF / expectancy | 0.077 / 0.02 / −1.05 R (random-walk data has no edge by construction) |
| regime minutes | reported per day (TREND / RANGE / NO_TRADE) |
| layering ON | 42 legs · 14 baskets · 2.86 legs/basket · worst basket loss $−115 (−1.16 %, forced M15-close exits can exceed the 0.75 % stop budget) |

Items 13/14 on the same generator (30 synthetic days, 5 hand-written events: CPI beat, NFP miss, FOMC, flat PCE,
Retail Sales miss) — mechanics only:

| block | result |
|---|---|
| sweep & reclaim | 253 marked (random walk sweeps constantly), 14 with bias, 3 caught → 3/3 scalp TPs (+1.01 R), 0 runners (pre-drop highs inside the scalp TP) |
| news PRE / RELEASE | every event blocked T−15 → T+5 (trace), open positions protected per policy |
| news POST | CPI beat → DOWN but first move UP → **not confirmed, no trade**; NFP miss → UP confirmed → 1 trade; FOMC first-15-min DOWN → 1 trade; PCE 0.0 surprise → NONE → daily bias; Retail Sales miss → UP → 1 trade — NEWS rows reported separately from the 13 normal-session trades |

#55 engine replay, 10 synthetic days: old arm 129 signals (78 BUY / 51 SELL), new arm 194 (103 / 91), 94 stale-feature
entries flagged; per-trade expectancy −0.73 R (new) vs −0.88 R (old) on noise. **The new arm trades MORE often** — the
"must not increase losses on choppy days" requirement is exactly why the flag ships OFF with a one-week shadow compare.

## Items → code
* 1 bias/hysteresis `gold_trend_pulse/rules.py: daily_bias, BiasTracker` · 2 entries `detect_entry` · 3 exits `plan_exit,
  breakeven_level, time_stop_due`, live `trade_manager._gold_trend_pulse_exits` · 4 filters `spread_ok, session_ok,
  cooldown_until, daily_stop, news_blackout` · 5 sizing `size_lots` (+ pipeline `risk_pct_cap` 0.25) · 6 modes/gate
  `engine.effective_mode, live_gate` · 7 reporting `routes/gold_trend_pulse_routes.py`, `GoldTrendPulseCard.jsx`,
  Market Intel + Strategy Report sections · 8 replay `gold_trend_pulse/replay.py`, `scripts/gtp_replay.py` · 11
  layering `layer_levels, basket_shared_stop, size_basket_leg, basket_invalidated`, `user_direction_cap` basket slot ·
  12 range `detect_range, regime, detect_range_entry, plan_range_exit, range_breakout` · #55 `intraday_features
  (live_price, m5_bars, as_of)`, `strategy_engines` override + live breakout check, `ai_signals` shadow compare,
  `streak_losses` stale exemption · 13 sweep `rules_news_sweep.detect_sweep_reclaim, plan_sweep_exit, split_runner_lots`,
  engine sweep branch + runner ticket, `trade_manager` runner BE (`be_trigger_price`) · 14 news `news_phase,
  news_surprise_bias, fomc_bias, news_confirmation, detect_news_entry, plan_news_exit, news_pre_action, next_news_event`,
  engine PRE/RELEASE/POST + `_news_protect`, `gtp_news_events` report rows, Market Intel plan line, `scripts/gtp_news_events.py`.
* #56 `loss_cooldown.py` (`is_real_loss`, `trade_r_multiple`, cross-account scan), `bot_runner` pulse / counter.
* Review #62–#70 fixed (trend-ride exclusion, fail-closed spread by broker symbol, demo-attested demo_live + enum + PATCH guard + rolling gate + basket-once, layered-leg checks, management during freezes + ack-based protection, complete M5 / completed H1-H4 / H4 warm-up / t_basis, sweep scalp cap, scratch outcomes + original-stop R + stored costs, exact event match) — `test_gold_trend_pulse_review_62_70.py` (9).
* Tests: `tests/unit/test_gold_trend_pulse.py` (20), `tests/unit/test_gold_trend_pulse_news_sweep.py` (16), `tests/unit/test_v1_60_57_cross_account_loss_cooldown.py` (4),
  `tests/unit/test_v1_60_57_engine_live_features.py` (7).
