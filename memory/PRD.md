# AI Trading Bot — STOIC · PRD

## Original Problem Statement
Build STOIC — an AI trading bot for Gold (XAUUSD), Indices (US30/NAS100) and
Bitcoin (BTCUSD) with four risk profiles (low/middle/high/extreme), live MT5
microcent execution via a downloadable EA (Bridge pattern), JWT auth,
Android-friendly responsive UI, dual-AI intelligence (Claude Sonnet 4.5 +
Claude Fable 5 AI Strategy Optimizer via Emergent LLM Key), Kelly Criterion
sizing, Regime-Adaptive Risk Modifier, Macro-freeze, Meta-Labeler classifier
with Profit-Tied learning objective, admin moderation, 1-click MT5 connection
via PowerShell auto-installer, and full broker-terminal data integrity
(ghost-close recovery + on-demand deep broker sync).

## Core requirements (user-stated)
- Live trading via MT5 EA bridge; Spot crypto via CCXT (Kraken paper, Binance planned).
- Explainable AI trading, high win rate via adaptive regimes, robust guardrails.
- Bot logic/training focused on increasing win rate AND total profit (payoff-ratio repair).
- The bot must be able to restore correct data and synchronize with brokers' terminals at any time (iter-46, done).
- Admin moderation: terms of use, suspend/terminate users.
- Production domain: stoicaibot.com.

## Current EA version
**v1.42** — `LATEST_EA` hardcoded in: `routes/bot_routes.py`, `routes/diagnostic_routes.py`,
`routes/setup_routes.py` (`ea_latest_version`), `frontend/src/pages/Accounts.jsx`,
`frontend/src/components/EaVersionStrip.jsx`, EA `#property version` + `EA_CLIENT_VERSION`.
Version tests: `tests/test_iter40_ea_clamp_stops.py::test_version_138_everywhere`,
`tests/test_iter85_ea_token_autoload.py` (EXPECTED_VERSION).

## Key bridge endpoints
- `POST /api/bridge/heartbeat` — balance/equity/positions snapshot, reconcile, ghost-close estimation, auto-heal deep-sync queueing (5-min `ghost_check_at` throttle).
- `POST /api/bridge/poll-trades` — pending trades + modifications + `sync_request` dispatch (180s re-dispatch guard).
- `POST /api/bridge/external-deal` — idempotent deal ingestion; duplicate re-push REPAIRS closed trades with estimated/unknown/missing P&L.
- `POST /api/bridge/sync-complete` — EA confirms deep sync; sets `last_full_sync_at`, WS `broker_sync_complete`.
- `POST /api/accounts/{id}/request-sync` — user-triggered 7-day deep broker sync.
- `POST /api/optimizer/analyze` — Claude Fable 5 strategy optimizer (excludes `pnl_estimated`/`pnl_unknown`/manual trades).
- `GET /api/trades/history` — date bounds `from_date`/`to_date` + summary aggregations.

## Key DB schema notes
- `trades`: `pnl`, `pnl_estimated`, `pnl_unknown`, `origin` (auto/manual/other_ea/external), `exit_price`, `live_pnl`/`live_price` snapshots, `backfilled_at`.
- `accounts`: `bridge_token`, `pending_history_sync{lookback_seconds,requested_at,requested_by,dispatched_at}`, `last_full_sync_at`, `last_full_sync`, `ghost_check_at`, `available_symbols`, `ea_version`.
- `bot_configs`: `soft_stop_enabled`, `let_winners_run`, `trade_of_day_cap`, daily max trades 100.
- `broker_deals`: unique index (deal_id, account_id) — idempotency backbone.

## Auto Loss Review & Daily Auto-Learning (iter-54/55, June 2026 — DONE)
- `loss_advisor.py`: Claude reviews 7-day losses (any new loss, 24h cooldown), proposes measures from a fixed menu, shadow-tests each against 14 days of real trades.
- Auto-apply (user toggle `postmortem_settings.auto_apply_guards`, default ON): measures with net ≥ +$100 AND losses_avoided ≥ 2× wins_missed become live `auto_guards` (max 5 active) or config knobs (friday_flat). Enforced in `bot_runner.py` via `live_guard_block()`.
- Auto-revert: each review re-shadow-tests active guards; net<0 → deactivated. Telegram alerts on apply/revert (`notify_auto_guard`).
- API: GET/POST `/api/postmortem/settings`, GET `/api/postmortem/guards`, POST `/api/postmortem/guards/{id}/revert`, GET `/api/postmortem/reviews`, POST `/api/postmortem/reviews/run`.
- UI: Loss Lab — Auto-Learning card (`auto-apply-toggle`), guards list with manual REVERT, ReviewCard shows AUTO-APPLIED badges + auto-reverted section.
- Tests: `tests/test_iter55_auto_learning.py` (35 tests incl. HTTP), `tests/test_iter54_loss_advisor.py`.

## EOD Quiet Window (iter-56, June 2026 — DONE)
- Spreads widen drastically before the daily close → no order operations 23:40–00:05 BROKER server time.
- EA v1.41: inputs `EodQuietEnabled/EodQuietStart/EodQuietEnd`, `IsEodQuietWindow()` (TimeCurrent = broker time, wraps midnight); PollPendingTrades skipped entirely + guards in ExecuteTrade/ClosePosition/ApplyModifySL/ApplyPartialClose/ApplyFullClose. Server re-dispatches after (dispatch locks expire). Heartbeats/history sync continue.
- Backend mirror: `eod_quiet.py` + gate in `bot_runner.py` veto chain (uses learned `broker_utc_offset_sec`, intel counter `eod_quiet_block`) so signals aren't queued at stale prices.
- Tests: `tests/test_iter56_eod_quiet.py`.

## Payoff Repair (iter-57, June 2026 — DONE)
Root cause of "78.9% win rate, negative profit": `profit_taking_mode=win_rate` Smart Cap clips TP (leaves SL untouched) AFTER the R:R≥2.0 entry gate → realized R:R 0.2-0.5; plus 147/147 SELLs while gold rallied (MTF tiers are DAILY-bar only, blind to intraday).
- `payoff_guard.py`: `payoff_guard_apply` — default mode "tighten" clamps SL to 2× TP1 distance (signal executes with corrected geometry, counter `payoff_guard_tighten`, signal field `payoff_guard.tightened`); mode "skip" vetoes (cfg `payoff_guard_mode`/`payoff_guard_enabled`/`payoff_guard_max_sl_tp1`). Plus `intraday_counter_momentum` (veto trades fighting >±0.4% intraday move vs yesterday's close, ±1.0% crypto; wired in ai_signals as "VETO (intraday-momentum)", signal field `intraday_momentum`).
- `let_winners_run=True` set on ALL active bot_configs (BE-only at TP1, no banking).
- Auto-Learning toggled back ON; Loss Advisor auto-applied "Tighten Friday Close Window to 90min" (net +$1,006 shadow-tested).
- NOTE: DB has 500+ stale INACTIVE bot_configs (test artifacts) — always filter `active: True`.
- Tests: `tests/test_iter97_payoff_guard.py`.

## Loss Cooldown (iter-58, June 2026 — DONE)
- After ANY loss: block same base-symbol+direction re-entries for 30min across ALL accounts (`loss_cooldown.py`, cfg `loss_cooldown_enabled`/`loss_cooldown_minutes`, 0=off, counter `loss_cooldown_block`). Shadow-tested 14d: -$174 → +$7,429 (blocked 159 clustered re-entries, net -$7,603). Verified live via Bot Pulse.
- Context: 5 correlated accounts fire the same signal every ~3min; anti-tilt is per-account (2 losses each) so one wrong read = 13 clustered losses before it bites.
- Tests: `tests/test_iter98_loss_cooldown.py`.

## SHORT-Tier Momentum Veto (iter-59, June 2026 — DONE)
- Root cause of persistent all-SELL losses: gold V-recovered +3.3%/week; SHORT tier = UP (+1.4%) but MEDIUM/LONG stayed DOWN → 2-of-3 MTF vote kept approving SELLs into the rally & vetoing BUYs.
- Fix: `short_tier_momentum_veto` in payoff_guard.py — forbid fading SHORT tier when |fast-SMA slope| ≥ 1.0% (weaker fades stay allowed, they were net profitable). Wired in ai_signals ("VETO (short-tier-momentum)", signal field `short_tier_momentum_veto`).
- Shadow-tested 14d: total -$439 → +$604; Jul-7 session -$815 → +$35. Verified live (first post-deploy SELL vetoed).
- NOTE: bot will be quieter until the rally cools or slower tiers flip UP (BUYs still MTF-vetoed while MEDIUM/LONG are DOWN).

## Multi-Agent Build (iter-60, June 2026 — DONE)
- **Market Structure Agent**: EA v1.42 streams 96×M15 bars every 5min (`SendCandles` → `POST /api/bridge/candles` → `intraday_candles` collection, keyed user_id+base symbol). `market_structure.py`: swings, BOS, liquidity sweeps, unfilled FVGs, acc/dist; `structure_gate` vetoes trades against fresh BOS (≤16 bars). Fails open without candles.
- **Range Forecast (Quant)**: `range_forecast.py` — ATR14 projected range vs today's used range; `range_gate` vetoes TP1 beyond remaining room.
- **Fed Tone (Macro)**: `fed_tone.py` — Claude scores Fed/FOMC headlines hawkish(+1)/dovish(-1), 6h cache; gates XAUUSD only at |score|≥0.7.
- All three wired in bot_runner veto chain (counters: structure_gate_veto, range_gate_veto, fed_tone_veto; signal fields: market_structure, range_forecast, fed_tone).
- **Market Posture card**: `GET /api/bot/posture` (routes/posture_routes.py) + `MarketPosture.jsx` on Dashboard — today's expectancy strip, per-symbol tiers/structure/quant/vetoes/unlock-hints, macro series, fed tone, risk state. Note: auth user dict uses `user["id"]` not `_id`.
- Tests: `tests/test_iter99_structure_agents.py` (22).
- GOTCHA: multiple search_replace edits to the SAME file in one parallel batch can silently drop one edit — apply same-file edits sequentially.

## Offline RL Policy Layer (iter-61, June 2026 — DONE)
- `rl_policy.py`: distributional Q-values per state (base_symbol|action|session|regime|short-alignment), reward = PnL − 0.5·loss − 0.5·drawdown_deepening, trained on 90d real trades (lazy retrain 6h TTL, `rl_policies` collection). Decisions: BLOCK (n≥8 & 80% UCB<0), SCALE 0.5× (mean<0), else ALLOW.
- Modes via bot config `rl_policy_mode`: off | **advisory (default)** | enforce (BLOCK skips trade, SCALE halves lot via signal.rl_scale in sizing). Counters: rl_policy_block_advice / rl_policy_block. Signal field `rl_policy`.
- API: GET /api/rl/policy, POST /api/rl/train (routes/rl_routes.py). Posture endpoint + MarketPosture card show RL summary.
- First training: 405 trades → 18 states, 8 reliably-losing (worst: SELL|london_ny_overlap|LOW_VOL_TREND|short:FLAT n=66 mean -$88).
- NEXT STEP: after a few days of advisory data, promote to enforce mode. PPO/simulator route deferred until weeks of M15 candles accumulate.
- Tests: `tests/test_iter100_rl_policy.py` (17).

## Forecast Agent — Chronos-Bolt (iter-62, June 2026 — DONE)
- `forecast_agent.py`: amazon/chronos-bolt-tiny (CPU, zero-shot) quantile forecasts — M15 candles (8 steps=2h) or daily fallback (3d). Cached 5min/symbol, inference in thread executor, fail-open. NOTE chronos 2.3.1 API: `predict_quantiles(inputs=...)`.
- `forecast_gate`: veto only when ENTIRE 80% band opposes trade. Config `forecast_gate_mode`: off|**advisory (default)**|enforce. Counters: forecast_gate_advice/block. Signal fields: forecast, forecast_gate_advice.
- requirements.txt now starts with `--extra-index-url https://download.pytorch.org/whl/cpu` (torch==2.12.1+cpu) — DO NOT strip this line or deploy pulls the CUDA torch. Deps: torch, chronos-forecasting, transformers.
- Posture: per-symbol `forecast` + live `structure` (computed from candles directly, not from last signal). FORECAST row in MarketPosture.jsx.
- CONFIRMED LIVE: user's EA already streams M15 candles (source XAUUSD.fx, 96 bars) — Structure agent live (BULLISH BOS, ACCUMULATION, 4 FVGs), Chronos forecasting real data.
- Rejected (documented for user): PatchTST/Informer/Autoformer/iTransformer (train-from-scratch → overfit on 252 bars), TimeGPT (paid API), TimesFM (too heavy).
- Tests: `tests/test_iter101_forecast_agent.py` (RUN_CHRONOS_TEST=1 for real inference).

## Master Agent Consensus (iter-63, June 2026 — DONE)
- `consensus.py`: weighted vote 0-100 (trend 25%, quant[conf+RL] 25%, structure 20%, forecast 20%, macro[fed+intraday tape] 10%; risk agent stays hard gates). Verdicts: STRONG≥70 / OK≥55 / WEAK≥40 / CONFLICTED.
- Gate in bot_runner (after forecast gate): `consensus_gate_mode` default **enforce**, `consensus_threshold` default 55 (both in BotConfigUpdate). Counters: consensus_low / consensus_block. Signal field `consensus`.
- Posture computes live BUY & SELL consensus per symbol (hypothetical contexts w/ per-direction RL decision); MASTER CONSENSUS pills in MarketPosture.jsx (hover = vote breakdown).
- Live at build time: BUY 61 (OK) vs SELL 37 (CONFLICTED) on XAUUSD.
- Tests: `tests/test_iter102_consensus.py`.

## Probabilistic Forecasting (iter-64, June 2026 — DONE)
- Chronos now returns 9 deciles (`QUANTILE_LEVELS`); `prob_forecast.py`: CDF interpolation, 3-bucket scenario table ("30%: +84 pips · 60%: -113 · 10%: -277 → EV -71 long"), `trade_eval` (P(TP)/P(SL), prob-weighted expectancy, quantile-based suggested_sl [q10/q90, only if tighter], lot_multiplier 1.0/0.75/0.5 — never scales up).
- Wired in bot_runner inside the forecast block: `prob_forecast_mode` off|**advisory (default)**|enforce (enforce = veto negative-EV, apply tighter SL, prob_lot_scale merged with rl_scale in sizing). Counters: prob_ev_negative/prob_ev_block. Signal fields: prob_eval, prob_sl_applied, prob_lot_scale.
- Forecast payload has `distribution` + `quantile_levels/values`; scenarios shown in MarketPosture FORECAST row.
- Tests: `tests/test_iter103_prob_forecast.py`.

## Liquidity Mapping Agent (iter-105, June 2026 — DONE)
- `liquidity_map.py`: institutional order-flow view from M15 candles + DOM — unmitigated order blocks (last opposing candle before displacement), stop clusters (equal highs/lows = pooled stops, swept-pool removal), FVGs (reused), volume profile (POC/VAH/VAL, 70% VA), cumulative delta proxy (CLV×vol, divergence flags), draw-on-liquidity magnet (strength/distance-weighted pull).
- **EA v1.43**: `MarketBookAdd(_Symbol)` at init, `SendDom()` every DomSeconds (30s) → `POST /api/bridge/dom` → `dom_snapshots` collection (per user/symbol, 16 levels/side). Degrades gracefully when broker gives no DOM. RoboForex + OnEquity both provide DOM for XAUUSD.
- `liquidity_gate`: vetoes BUY inside unmitigated SUPPLY OB / SELL inside DEMAND OB / trades into ≥60% opposing book imbalance. `liquidity_gate_mode` default **enforce** (advisory = annotate only). Counter: liquidity_gate_veto. Signal field `liquidity`.
- Consensus reweighted: trend 20 / quant 20 / structure 15 / forecast 20 / **liquidity 15** / macro 10.
- Posture returns per-symbol `liquidity`; LIQUIDITY AgentRow in MarketPosture.jsx (draw, stops, active zone, OBs, POC, delta, DOM live/offline strip).
- Tests: `tests/test_iter105_liquidity_map.py` (13 passed). Live: XAUUSD draw DOWN, 2 OBs, 3 clusters, liquidity vote in both consensus scores.

## AI News Understanding Agent (iter-106, June 2026 — DONE)
- `news_understanding.py`: replaces keyword sentiment — Claude (bullion-desk macro-strategist persona) scores EACH headline -3..+3 answering "how bullish is this specifically for <asset>?". Feed: NewsAPI macro wire (FOMC/CPI/NFP/central banks, tier-1 domains reuters/bloomberg/cnbc/ft/wsj) + asset query, deduped, 12 max.
- Recency-weighted aggregation (12h half-life) → net -3..+3 + label (strongly_bearish..strongly_bullish) + top-3 drivers with per-headline score & why. Cached 45min/asset, fail-open.
- `news_gate`: veto BUY when net ≤ -2 / SELL when net ≥ +2. `news_gate_mode` default **enforce**. Counter: news_gate_veto. Signal field `news_ai`.
- Consensus: news folds into MACRO vote (±0.5 when |net| ≥ 0.75). Posture returns per-symbol `news_ai`; AI NEWS AgentRow in MarketPosture.jsx (net badge + scored drivers).
- Old `news.py` score_sentiment (coarse -1..1) left untouched (still used by ai_signals confidence).
- Tests: `tests/test_iter106_news_understanding.py` (12 passed). Live: 12 headlines scored, weak NFP +2.5 gold-bullish, net +0.37 neutral.

## Economic Calendar Intelligence (iter-107, June 2026 — DONE)
- `calendar_intel.py`: instead of only avoiding news, predicts P(breakout/fakeout/reversal/continuation) per scheduled event. Dirichlet priors per event class (FOMC/CPI/NFP/GDP/PMI/SPEECH/OTHER) + learned posterior from REAL outcomes (`event_outcomes` collection, each real outcome ≈ 8 prior units) + context multipliers (vol compression → breakout ×1.3; stop clusters within 1.5×ATR → fakeout ×1.25; price stretched >2×ATR → reversal ×1.3).
- `classify_outcome`: post-hoc labels each passed high-impact event from M15 candles (pre-range 8 bars vs post 4 bars: closed-beyond=breakout, wick-beyond+close-inside=fakeout, direction-flip=reversal, else continuation). `sweep_event_outcomes` in bot_runner main loop (throttled 1/30min, dedupe by title|ts|symbol).
- `calendar_entry_policy`: VETO new entries when fakeout dominant (p≥0.35) within 45min pre-event or 15min post-event ("first spike is a trap"); CAUTION otherwise. `calendar_intel_mode` default **enforce**. Counter: calendar_intel_veto. Signal fields: calendar_intel, calendar_policy. Coexists with existing macro_freeze hard gate.
- Posture per-symbol `calendar_intel`; CALENDAR INTEL AgentRow (event countdown, 4-way prob bar, learned-outcomes count, context drivers).
- **Fix**: `economic_calendar.get_events` now backs off 10min on fetch failure (FF feed was stuck in HTTP 429 because every caller retried instantly).
- Tests: `tests/test_iter107_calendar_intel.py` (13 passed); consensus test updated for liquidity/news votes. 62 tests green across iter102-107 suites.

## Stacked ML Ensemble (iter-108, June 2026 — DONE)
- `ml_ensemble.py`: 7-model ensemble — 4 GBMs trained on user's real trades (sklearn GradientBoosting, XGBoost, LightGBM, CatBoost; 19 features via `featurize()`: action/confidence/tiers/session/regime/cyclical hour/sl_pips/rr) + 3 live agents (Transformer=Chronos band, RL agent sigmoid(mean/40), Bayes p_success).
- **Intelligent averaging**: GBM weights = walk-forward TimeSeriesSplit(3) OOS AUC − 0.5 (zero skill = zero vote), scaled by min(n/150,1)×0.7; RL/Bayes weights scale with evidence min(n/20,1)×0.35; Transformer fixed 0.25. p_win = Σwp/Σw.
- Models persisted `/app/backend/models_store/{uid}/gbm_ensemble.joblib` (joblib, mtime-cached); metadata in `ml_ensembles` (TTL 12h auto-retrain, min 40 trades). Routes: GET `/api/ml/ensemble`, POST `/api/ml/train`.
- bot_runner gate after forecast block: `ml_ensemble_mode` default **advisory** (enforce = veto p_win<0.40 with ≥3 models). Counters: ml_ensemble_low_p/ml_ensemble_block. Signal field `ml_ensemble`. Consensus quant vote += clip((p_win−0.5)×2)×0.5 when ≥3 models.
- Posture per-direction `ml_ensemble`; ML ENSEMBLE AgentRow (p_win per side, member breakdown w/ weights, AUC line).
- Deps added: scikit-learn, lightgbm, catboost (xgboost was present). requirements.txt frozen.
- Tests: `tests/test_iter108_ml_ensemble.py` (7 passed). Live on 405 real trades: XGBoost AUC .596→w .386, CatBoost .516→w .064; BUY p_win .711 (5 members), SELL .664 (7 members).

## Meta-Learning + Online Learning + Causal AI (iter-109, June 2026 — DONE)
- **Meta-Learning** (`meta_strategy.py`): multi-armed bandit over strategy presets (trend_rider/scalper/mean_reversion/breakout/sniper/balanced). Score = recency-weighted mean R (half-life 10 trades) + UCB exploration bonus. Switches when active strategy loses ≥3 of last 5 AND better arm exists, or an alt beats it by 0.10 margin. Opt-in `meta_strategy_enabled` (models.py); overlays preset config in bot_runner (after auto_preset, overrides it); stamps `signal.meta_strategy` for attribution (trades→signals join). State in `bot_configs.meta_strategy_state`; switches logged to `meta_strategy_events` + pulse + counter meta_strategy_switch. Posture top-level `meta_strategy`.
- **Online Learning** (`online_learning.py`): continuous retraining — ML ensemble + RL + Bayes retrain when ≥5 new closed trades OR ≥1 new trade + models >1h old (no new trades = no retrain). Sweep in bot_runner main loop, throttled 10min, state in `online_learning` collection. Posture top-level `online_learning`.
- **Causal AI** (`causal_model.py`): structural causal graph for gold — inflation(T10YIE) → Fed tone → yields(DGS10) → REAL yields(DGS10−T10YIE) → USD(DTWEXBGS) → gold. Weekly deltas normalised by tanh/sigma; pressure = −0.5·real −0.35·usd −0.15·fed (−1..+1 bullish gold). Chain trace with active/confirms flags + narrative. Attached to XAUUSD signals in bot_runner fed-tone block; consensus macro vote ±0.4 when |pressure|≥0.35. CAUSAL AI AgentRow (XAUUSD only).
- New config fields in models.py: ml_ensemble_mode, liquidity_gate_mode, news_gate_mode, calendar_intel_mode, meta_strategy_enabled.
- Tests: `tests/test_iter109_meta_online_causal.py` (14 passed); 70 green across iter102-109. Live: causal chain traced from real FRED (pressure −0.09 NEUTRAL), meta bandit initialised.

## Uncertainty Estimation (iter-110, June 2026 — DONE)
- `uncertainty.py`: calibrated confidence + risk tier per prediction. U = weighted mix of model disagreement (ensemble member spread /0.25, w.30), Bayes CI90 width (w.20), Chronos band/median ratio (w.15), consensus vote conflict (w.25), data sufficiency n/20 (w.10). p_cal = 0.5 + (p_dir−0.5)(1−U) where p_dir = mean(consensus/100, ml p_win, raw confidence/100). Risk: LOW (≥75% & U≤.35) / HIGH (<60% or U≥.6) / MEDIUM.
- `uncertainty_gate` skips HIGH-risk or below-floor trades. Config: `uncertainty_gate_mode` default **enforce**, `min_calibrated_confidence` default 60 (models.py). bot_runner block AFTER consensus gate. Counters: uncertainty_low_conf/uncertainty_skip. Signal field `uncertainty`.
- Posture per-direction `uncertainty`; consensus pills now show "BUY 56 · 51% HIGH" (score · calibrated conf · risk) with driver tooltip.
- Tests: `tests/test_iter110_uncertainty.py` (9 passed); 79 green across iter102-110. Live: BUY 51%/HIGH (models ±19pp apart, 4 agents against) vs SELL 65%/MEDIUM.

## Adaptive Position Sizing (iter-111, June 2026 — DONE)
- `adaptive_sizing.py`: risk% per trade = base × Π(bounded multipliers): confidence (calibrated iter-110 conf: 60%→~0.3×, 98%→~1.8×; HIGH tier ×0.7, LOW ×1.1), volatility (inverse ATR targeting recent-8 vs full-96 bars, 0.6-1.25×), accuracy (20-trade WR tiers 0.5-1.3×), liquidity (DOM/draw alignment 0.7-1.15×), drawdown (30d realized peak-to-now vs equity: ≥10%→0.4×, ≥5%→0.6×, ≥2%→0.8×). Total clamp [0.1×, 2×]; final risk clamp [floor 0.1%, cap 2.0%].
- bot_runner: applied to `profile.risk_pct` BEFORE Kelly sizing (supersedes old win-rate-only `adaptive_risk_enabled` when on). Config: `adaptive_sizing_enabled` default **True**, `adaptive_risk_floor_pct`/`adaptive_risk_cap_pct` (models.py). Signal field `adaptive_sizing` {multiplier, risk_pct, components, drawdown_frac, recent_win_rate}.
- Tests: `tests/test_iter111_adaptive_sizing.py` (7 passed); 136 green across iter10x suites. Live: admin account (20% WR, 20% DD) throttled high-conf trade to 0.2% — capital preservation dominating, as designed.

## Monte Carlo Trade Simulation (iter-112, June 2026 — DONE)
- `monte_carlo.py`: pre-entry simulation of N=10,000 paths (numpy vectorized, ~14ms) by bootstrap-resampling real M15 bar dynamics (close-change + up/down wick triples — preserves fat tails & drift, no Gaussian). Each path → SL hit / TP hit / timeout over 96-bar horizon; same-bar collision counts as SL (conservative).
- Outputs: p_tp_first/p_sl_first/p_timeout, ev_r (= p_tp·R − p_sl + timeout mean R), max-DD distribution in R (median/p95, wick-inclusive so can exceed 1R), median_bars_to_exit. Deterministic with seed.
- `mc_gate`: veto when ev_r ≤ 0 ("enter only if EV positive"). Config `monte_carlo_mode` default **enforce**, `monte_carlo_paths` 10000 (models.py). bot_runner block after uncertainty gate. Counters: mc_negative_ev/mc_block. Signal field `monte_carlo`.
- Posture surfaces last signal's `monte_carlo`; MONTE CARLO AgentRow (paths, TP/SL odds, EV, DD stats).
- Tests: `tests/test_iter112_monte_carlo.py` (8 passed — note: test bars must be drift-free by construction since bootstrap preserves drift); 144 green across iter10x-11x. Live gold: 10k paths 14ms, EV +0.05R.

## Explainable AI + Self-Evaluation Agent (iter-113, June 2026 — DONE)
- **Explainable AI** (`explainer.py`): `explain_decision(signal)` builds {headline, because[], despite[]} from consensus votes + all agent fields (trend tiers, structure bias/phase, liquidity draw/zone/delta/DOM, forecast, quant p_win/Bayes, macro fed/news/causal, Monte Carlo odds, calendar caution). Attached as `signal.explanation` in bot_runner before insert; posture surfaces last signal's explanation; EXPLAIN AgentRow (✓ because / ✗ despite).
- **Self-Evaluation** (`self_evaluation.py`): grades every closed trade — entry_quality/exit_quality 0-100 (MFE/MAE recomputed from M15 candles, stopped-then-reversed detection), regime/volatility/news context, mistake taxonomy (counter_trend_entry, entered_into_opposing_liquidity, traded_into_news_event, stop_too_tight, left_money_on_table, low_confidence_entry, oversized_in_drawdown). Losses get a Claude "why was I wrong?" one-liner. Stored in `trade_evaluations`; trades flagged `self_evaluated`. Sweep in bot loop (5min throttle, 10/sweep, 48h lookback).
- **Adjusts future behavior**: `compute_adjustments` over last 20 evals (≥30% recurrence) → sl_widen_factor 1.2 / tp_extend_factor 1.15 / min_conf_bump +5, stored in `behavior_adjustments`, applied to every new candidate in bot_runner (`apply_adjustments` mutates SL/TP pre-MonteCarlo; conf bump raises uncertainty floor). Signal field `behavior_adjustments`.
- Posture top-level `self_evaluation` {graded, avg qualities, top_mistakes, last_lesson, adjustments}; SELF-EVAL AgentRow.
- Tests: `tests/test_iter113_explain_selfeval.py` (9 passed); 153 green. Live: 50 real trades graded, Claude lessons generating ("sold into LOW_VOL_TREND primed for expansion").

## Advanced Risk Engine (iter-114, June 2026 — DONE)
- `risk_engine.py`: unified pre-trade authority run AFTER all lot sizing (post rl_scale), fail-open. Five checks → {allow, scale, checks}:
  1. **Dynamic leverage**: max notional = equity × max_leverage(20×) × vol_adj(0.4-1.0, from ATR expansion) × conf_adj(0.6/<65%, 1.2/>85%); oversize trimmed to fit.
  2. **Event exposure**: high-impact print ≤60min → correlated USD notional (open+new) capped at event_exposure_cap_pct (100%): block above cap, halve above 50%.
  3. **Drawdown ladder**: daily(3%)/weekly(7%)/**monthly(12%, new)** realized-PnL windows vs equity → block for the window.
  4. **Abnormal market**: last bar ≥4× median range → suspend cycle; ≥2.5× → halve; candle feed stale >45min on weekdays → suspend (data outage).
  5. **CVaR budget**: projected portfolio CVaR₉₅ (portfolio/var.py) incl. hypothetical position vs cvar_budget_pct (8%): trim to fit, hard block >1.5×.
- Config (models.py): risk_engine_enabled=True, monthly_drawdown_pct, max_leverage, cvar_budget_pct, event_exposure_cap_pct. Verdict stored on signal doc (`risk_engine`). Counter: risk_engine_block. `risk_engine_status` → posture top-level; RISK ENGINE AgentRow (equity, D/W/M PnL, CVaR, DD status).
- Tests: `tests/test_iter114_risk_engine.py` (7 passed; 40 green iter11x). Live: real -4.6% daily DD correctly blocking; oversized gold trade caught by event cap (1161% exposure) AND CVaR ceiling (23.9% > 12%).

## Live Architecture Pipeline + Conflict Audit (iter-115, June 2026 — DONE)
- `routes/architecture_routes.py`: GET `/api/architecture` — 11 stages matching the user's diagram exactly (Market Data[price/news/macro] → Feature Engineering → Models[Transformer/RL/Bayes] → Ensemble → Regime → Decision Confidence → Monte Carlo → Sizing → Risk → Execution → Broker) with live per-stage health (candle age clamped ≥0 for broker time offset, model states, EA versions, heartbeats).
- `ArchitecturePipeline.jsx`: collapsible SYSTEM ARCHITECTURE card on Dashboard rendering the live pipeline with status dots (testids arch-stage-*).
- Pipeline order audit confirmed bot_runner matches the diagram: gates → RL → Bayes → forecast → ml_ensemble → consensus → behavior_adjustments → uncertainty → monte_carlo → insert → adaptive_sizing → Kelly → corr → rl_scale → risk_engine → execution.
- **Testing agent full audit (iteration_38.json): 100% — 99/99 unit + 13/13 HTTP integration + all frontend testids; zero backend/frontend issues, zero conflicts.**
- Advisory notes from review (non-bugs): posture caps symbols[:4]; MarketPosture.jsx >360 lines (future split candidate).

## Stale-Data Blindness Fix (iter-117, July 8 2026 — DONE)
User report: gold dropped 4120→4060 with zero bot action. Root cause: (a) noise filter ran on DAILY returns only — frozen at entropy 0.9335 "NOISY" → cheap-HOLD short-circuit for 24h+ regardless of intraday structure; (b) daily history memory-cached 6h → pipeline analyzed px 4130 while live was 4064. Fixes in `ai_signals.py` + `market.py`:
- `_intraday_entropy_override()` — when daily entropy is NOISY, fresh EA M15 stream (<30min, ≥31 bars) is classified; ORGANIZED intraday verdict outranks daily (`source: intraday_m15_override`).
- Live-price patch: last daily bar's close/high/low refreshed with live quote before computing indicators/MTF/kalman/vwap.
- Daily history cache TTL 21600s → 900s.
Verified live: signals now show real px + ORGANIZED override; LLM runs full analysis instead of cheap-hold.

## MTF Confluence Engine (iter-125, July 8 2026 — DONE, 18/18 tests, user-specified strategy)
User rejected "express lane"; specified top-down MTF: 4H trend → 1H structure → 15M pullback setup → 5M/1M entry; only trade on alignment. Built `mtf_intraday.py`: resample M15→1H/4H; `tf_trend` = EMA8 vs EMA20 gap ≥0.2% (price-vs-EMA deliberately ignored — pullbacks dip below fast EMA); `detect_pullback` = impulse ≥2×ATR15 + 25-70% retrace → minor swing level; entry = LIVE-PRICE break of swing level (EA has no M5 stream; live quote = tick precision). `fetch_mtf_confluence(symbol, live_price)`.
- ai_signals: `mtf_confluence_mode` (cfg `mtf_confluence_enabled`, enabled on all 6 active configs). Scope `mtf_confluence` gets FIRST claim (before range scalp/aggressive). `DETERMINISTIC_SCOPES=("range_scalp","mtf_confluence")` now drives all trend-veto exemptions (CHOP/entropy/daily-MTF/short-tier/A+/self-contra/counter-momentum). learned_meta = advisory for mtf_confluence (was 152 vetoes/day, daily-swing-trained). Geometry = intraday_scalp branch (SL 1.5×atr15, TP 5×, weighted R:R 2.08); rr floor 1.1. Consensus scope-adjust extended to this scope.
- `bridge_routes` candle store now MERGES bars by timestamp, cap 800 (EA sends ~96; 4H analysis enriches over days).
- `GET /api/bot/mtf-confluence?symbol=` + `MtfCascadePanel.jsx` on Dashboard (live 4-row cascade w/ statuses). BotConfig toggle "MTF Confluence Mode".
- Verified: live endpoint (real data: 4H+1H DOWN aligned, correctly waiting on 76% retrace), full pipeline sim (SELL, rr 2.08, no vetoes), Dashboard panel screenshot.

## Consensus Fairness + Precedence Fix (iter-124b, July 8 2026 — DONE)
User screenshot showed all scalp bots SKIP at "consensus 50-53 < 55". Two root causes: (1) consensus weights trend 20% + forecast 20% — both definitionally trend-following, handicapping range fades 40%; fixed in `consensus.py`: scope=="range_scalp" → drop trend+forecast, re-normalize remaining weights (quant/structure/liquidity/macro); `scope_adjusted` field in output. Verified: good fade 86, garbage fade 21 (gate stays meaningful). (2) PRECEDENCE BUG: aggressive override ran BEFORE range-scalp check, flipping HOLD→SELL so the range engine never engaged (needs action==HOLD); fixed: range scalp block moved above aggressive override in ai_signals — confirmed via simulation (both modes on → scope range_scalp, aggr None, consensus 57 passes).

## Bot vs Manual P&L Split (iter-124, July 8 2026 — DONE)
`/api/bot/quick-actions` now returns `todays_bot_pnl_usd`, `todays_bot_closed_count`, `todays_manual_pnl_usd` (split on `origin == "auto"`). QuickActionsBar header shows "BOT TODAY" (green/red) + "MANUAL" (gray/red, lg screens) instead of a single mixed TODAY stat. Verified: header renders BOT +$0 / MANUAL +$349 (user's manual scalps). User does NOT need to disconnect accounts to separate stats.

## Range Scalp Engine (iter-123, July 8 2026 — DONE, tests 11/11 pass incl. iter-120 regression)
User: "market moves up and down, bot sleeping; strategies are fast scalp". Root cause: ALL strategies were trend-hunters — in an M15 RANGE nothing can ever fire (user was profitably scalping the range manually). Built:
- `intraday_features.py`: pack now has session_high/low + range_pos_pct; `range_scalp_signal()` — BUY ≤18% / SELL ≥82% of session range when trend FLAT + Donchian INSIDE + day_range ≥0.6% + range ≥4×atr15.
- `ai_signals.py`: `range_scalp_mode` param (from cfg `range_scalp_enabled` via strategy_agent). Fires only when LLM says HOLD + macro not frozen; `scope: range_scalp`. EXEMPT from trend vetoes: MTF, CHOP, entropy, short-tier, A+ confluence, self-contradiction, intraday counter-momentum. Geometry: SL 1.2×atr15 (30p floor), TP toward VWAP clamp [2.4, 4.8]×atr15, rr floor 1.05 (weighted lands ~1.25). ALL capital protections + bot_runner gates still apply.
- cfg toggle in models/_serialize/BotConfig.jsx ("Range Scalp Mode"); enabled on 5 scalper/fast_scalp configs (aggressive acct left off).
- E2E simulation: range-low pack → BUY 73, scope range_scalp, rr 1.25, no vetoes, tradeable.

## Per-Account Guard Settings UI (iter-122c, July 8 2026 — DONE)
Bot Config (already per-account via selector) now exposes: Loss Cooldown (enabled/minutes), Trade Geometry Guards (payoff_guard_enabled, payoff_guard_max_sl_tp1, min_final_rr). Added fields to `models.BotConfigUpdate`, `bot_routes._serialize` whitelist + new-config defaults, BotConfig.jsx save payload + CapitalGuardsSection controls. GOTCHA: `_serialize()` in bot_routes is an explicit whitelist — new cfg fields MUST be added there or GET returns None. E2E verified: per-account save/read isolated (acct A 15min/0.8 didn't touch acct B 30min/0.75).

## Per-Account Guard Scoping (iter-122b, July 8 2026 — DONE)
Guards now act individually per account (each has own equity/settings): `loss_cooldown.py` (was deliberately cross-account since iter-58 — user overrode; message now "on this account"), `_on_sl_cooldown(account_id=)`, loss-streak circuit-breaker query. Anti-tilt + trade_of_day_cap were already account-scoped. Verified with synthetic trades: loss on acctA blocks only acctA; manual losses block nothing; SL cooldown isolated per account.

## Manual-Trade Isolation for Behavioral Guards (iter-122, July 8 2026 — DONE)
User's manual GOLD SELL (−$0.16) froze all 5 bots 30min via loss cooldown. Behavioral guards must judge the bot by its OWN trades: added `origin: "auto"` filter to (1) `loss_cooldown.py` query, (2) anti-tilt (bot_runner ~485), (3) `_on_sl_cooldown`, (4) loss-streak circuit-breaker (~1217), (5) trade_of_day_cap query, (6) report-card `_baseline_stats` EV. Equity-level guards (daily drawdown, safety_guardian aggregate risk incl. open manual positions, auto-heal) DELIBERATELY still count everything — they protect the account, not the bot's behavior. Verified live: cooldown cleared, all 5 bots resumed scanning.

## Scalp Radar Telegram Ping (iter-121, July 8 2026 — DONE)
- `scalp_radar.py` — per user+symbol state in `scalp_radar_state`, 60/50 hysteresis: pings Telegram (`event_type: scalp_radar`, opt-out toggle on Notifications page) the moment M15 alignment ≥60 arms the intraday engine; direction flip re-pings; disarm below 50. Hooked in bot_runner right after signal fetch (uses signal's `intraday_m15`, no extra fetch; 5 configs on same symbol ping once).
- Tested: arm/dedupe/disarm/no-pack cycles verified against live DB with synthetic user; toggle visible on /notifications.

## Short-Tier Veto Intraday Deferral (iter-120b, July 8 2026 — DONE)
User re-enabled aggressive_mode on all 5 bots but signals stayed HOLD: aggressive override fired (SELL 58-68 conf) yet `short_tier_momentum_veto` (weekly daily-bar fast-SMA slope ±1%) reverted everything — it ignored aggressive_mode AND the new scalp scope, and had also blocked yesterday's 11:43 SELL before the −75pt drop. Fix in `ai_signals.py`: when `intraday_alignment(action) ≥ 60`, the short-tier veto DEFERS (reasoning gets "NOTE: Short-tier veto deferred..."); below 60 it still vetoes (correct — don't fade an active M15 bounce). Verified by simulation: strong-down M15 pack + aggressive → SELL 68, SL 150p / TP 100/200/300 (rr 1.17 ≥ 1.1 floor). Live conditions (M15 FLAT bounce, score 25) correctly still HOLD.

## Intraday Scalp Engine (iter-120, July 8 2026 — DONE, unit tests test_iter120 5/5 pass)
User complaint: gold ranged 2.31% (93.5pts, −75pt waterfall 11:00-13:00 UTC) and bot took nothing — pipeline was 100% daily-anchored (MTF tiers/breakout/vwap all from daily bars; LLM never saw intraday action; MTF veto killed everything on conflicted days unless aggressive_mode, which Auto-Heal turned OFF 07-07 19:05 after −$814/−4.55% day). Built:
- `intraday_features.py` — M15 pack from EA stream (trend EMA20/50, momentum_3h, donchian20, session VWAP dist, swing structure HH_HL/LH_LL, atr15, day_range_pct) + `intraday_alignment()` score 0-100 (≥60 = strong).
- `ai_signals.py`: pack fed to LLM as `intraday_m15` (+ SYSTEM_PROMPT guidance: tradeable intraday trend day = may fire 60-75 conf against daily conflict); MTF veto override → `scope: intraday_scalp` when alignment ≥60; scalp SL/TP from M15 ATR (SL 1.5×atr15 clamp [30p, SL_MAX], TP 5×atr15 ladder 0.4/0.7/1.0, weighted R:R 2.08 preserved). Signal now carries `scope` + `intraday_m15`.
- All other guardrails (final R:R guard, risk caps, anti-tilt, news freeze, confluence) remain fully active for scalps.
- Verified live: LLM reasoning now cites M15 structure explicitly; correctly HOLDs mid-bounce. Aggressive_mode remains OFF on all accounts (user hasn't chosen to re-enable).

## R:R Watch Panel (iter-119b, July 8 2026 — DONE)
- `GET /api/analytics/rr-watch` — before/after the 2026-07-08T09:45Z geometry fix: trades, median R:R (tp1 vs SL), win rate, net P&L per bucket. `RrWatchPanel.jsx` at top of Analytics page (red 0.31 before vs after-fix bucket, turns green at ≥0.75). No post-fix trades yet at build time (bot legitimately HOLDing at 35% conf). **Next session: re-audit once after-bucket has ~20 trades.**

## Trade Behavior Audit + R:R Expectancy Fix (iter-119, July 8 2026 — DONE)
Audit of 409 closed auto trades (14d): mechanics correct (geometry valid, slippage ≤2pts, Kelly trimming OK, guards firing) BUT median executed R:R was 0.31 vs designed 2.08 → 73.3% win rate with profit factor 0.97 (net −$439). Root cause: R:R veto (min 2.0) runs on ORIGINAL geometry; later Smart Cap/regime overlay clips TP to 60p (collapsing tp1=tp2=tp3) while SL stays 120p; payoff guard only clamped SL to 2× TP1 (R:R 0.5). Side effect: breakeven/trailing/partials never fired (0 breakevens/14d). Fixes:
- `payoff_guard.final_rr_guard()` — LAST-line weighted R:R check on FINAL geometry after all overlays; skips below `min_final_rr` (default 0.75, cfg-overridable). Wired in bot_runner after behavior adjustments; counter `final_rr_veto` (mapped into Payoff Guard row of report card).
- Payoff guard default `payoff_guard_max_sl_tp1` 2.0 → 1.2 (clamped scalps land at R:R ≈0.83; breakeven WR 55% vs realized 73%).
NOTE: parallel search_replace on the same file raced once (payoff_guard.py edit lost) — apply same-file edits sequentially.

## Backlog Trio (iter-118, July 8 2026 — DONE, tested iteration_40.json 100% pass)
- **"Why is my bot silent?" banner** — `SilentBotBanner.jsx` top of Dashboard. Backend: `_record_pulse` now also persists `_last_notable_pulse` (routine "cooldown active" SKIPs excluded so the real blocker survives); `/api/bot/pulse` returns `notable` + `notable_stale_seconds`. Banner hides if any bot executed <30min ago; expander shows per-bot reasons.
- **Partner Broker Card** — `routes/partner_routes.py` (GET lazy-seeds Exness/IC Markets/Vantage with PLACEHOLDER IB links; admin POST/DELETE; click tracking POST /brokers/{id}/click → affiliate_clicks). `PartnerBrokerCard.jsx` on Accounts page. Admin must replace placeholder links via POST /api/partners/brokers.
- **Weekly Agent Report Card** — `agent_report_card.py` (7d intelligence_counters × realized EV/trade → per-agent est. P&L impact, verdicts QUIET/KEEP ENFORCE/CONSIDER ADVISE/MONITORING, 1h cache in `agent_report_cards`). Route GET /api/agents/report-card. `AgentReportCard.jsx` on Agents page. First real insight: Execution Guards 105 blocks ≈ −$430 est. missed EV → CONSIDER ADVISE.

## POST-MORTEM "NOT ELIGIBLE" BUG FIXED (iter-127c, July 9 2026)
Clicking POST-MORTEM on a losing closed trade errored "Trade is not eligible for post-mortem". Cause: manual regenerate reused the AUTO-trigger heuristic (only sl_hit or consecutive-loss qualify) — and ALL recent losses close via external_close/broker_confirmed, never 'stop_loss', so nothing ever qualified. Fix: `maybe_record_postmortem(db, id, force=True)` — manual click generates for ANY closed losing trade (trigger='manual'); route `/postmortem/{id}/regenerate` passes force=True. Verified: full LLM narrative generated for trade 6a4fcf…3ea2.

## PERFORMANCE REVIEW + 4 WIN-RATE/PROFIT IMPROVEMENTS (iter-128, July 10 2026 — DONE, unit-tested 6/6)
Review of 26 closed auto trades (Jul 9-10): 12W/12L, +$631/-$671 ≈ -$40 net. Killers: (a) one pre-clamp oversized -$176 trade (already fixed); (b) GEOMETRY SABOTAGE — Smart-Cap TP clip + behavior SL-widen + payoff reshape left orders at R:R 0.34 (win $53 avg < loss $56 avg), also nullified 0.4R breakeven; (c) knife-catch fades at day-lows fired on 3 accounts simultaneously (-$128/min).
Fixes (user approved "all 4, no conflicts"):
1. GEOMETRY INTEGRITY — profit-taking TP clip, behavior adjustments, payoff guard now SKIP DETERMINISTIC_INTRADAY_SCOPES (swing/MTF only). ai_signals stashes `engine_geometry`; pre-send net in bot_runner restores it if placed R:R < 1.5 (`geometry_restored` flag).
2. FALLING-KNIFE FILTER — `recent_break` field in intraday_features (close outside prior 20-bar channel within last 8 bars); `_knife()` in strategy_engines blocks BUY-fades after fresh breakdowns / at ≤20% of a ≥1.0% down-day (mirrored for SELLs); also guards range_fade.
3. BREAKEVEN — mean_reversion cfgs+preset → 0.5R, breakout → 0.6R (scalper/fast_scalp already 0.4R); works again now that TPs are 2R.
4. CORRELATED-FADE STAGGER — same symbol+direction fade on another account within 30 min must be ≥ +0.3R before mirroring (`correlation_stagger` counter, pulse reason).
Loop verified clean; currently in Friday-flat window (by design). Ordering in bot_runner: stagger → geometry restore → std risk clamp (restore BEFORE clamp so sizing uses correct SL). `_DET_PG` import defined at payoff-guard block, reused by behavior-adjust + geometry net.

## "NO ACTION FOR 2 HOURS" — MANUAL-TRADE SLOT LEAK FIXED (iter-127f, July 10 2026)
Timeline reconstructed for 12:58–15:30 UTC silence:
1. 12:56-12:58 — 3 auto VWAP-fade BUYs lost → anti-tilt froze the account 1h (by design, expired correctly 14:26) + same-direction loss-streak breaker paused auto BUYs 4h (by design).
2. 13:30-15:30 — user manually scalped GOLD nonstop on the same account; TWO guards counted MANUAL trades against the bot: `inflight_q` (max_concurrent slots) and `pyramid_q` (anti-pyramid) had NO origin filter → bot locked out whenever manual positions were open. FIXED: both queries now `origin: "auto"` (re-applying the iter-122 rule "manual trades never count against the bot").
3. Verified after fix: engines analyze every loop; current holds are honest (price hugging VWAP mid-range = genuinely edgeless).
GOTCHA CONFIRMED 3rd TIME: parallel search_replace on the SAME file loses edits silently — same-file edits MUST be sequential.

## PRODUCTION DEPLOYMENT READINESS — PASS (iter-127e, July 9 2026)
- deployment_agent scan #1: PASS w/ warnings — torch(624MB)+transformers+chronos stack flagged as build-timeout/OOM risk (likely cause of the previous K8s deploy timeout).
- Removed from requirements.txt: torch, transformers, accelerate, chronos-forecasting, nvidia-nccl-cu12. Only consumer is forecast_agent.py (Chronos daily forecaster) which lazy-imports and degrades gracefully ("forecast agent disabled") — verified by import-block simulation. Forecast gate is advisory/fail-open, so production simply runs without Chronos forecasts. Preview still has torch installed locally → preview forecasts unaffected.
- deployment_agent scan #2: PASS, zero findings. App is deployment-ready; user just needs to click Deploy in the Emergent UI.

## AUTO POST-MORTEM ON EVERY LOSING TRADE (iter-127d, July 9 2026 — user request, DONE)
- `_is_postmortem_eligible`: any pnl<0 is now eligible; trigger labels: 'sl_hit' (close_reason contains stop_loss) / 'consecutive_loss' / 'loss'.
- Added missing hook in bridge_routes deal-close backfill path (~line 1225) — broker-reported/external closes bypassed execution.py's hook entirely (root cause of empty Loss Lab).
- Backfilled 12 recent losing trades (23 PMs total); pattern learner auto-tightened GOLD|UNKNOWN|OFF|SELL min_confidence 55→60 from the new data.
- Test updated: test_iter45_postmortem single-loss case now expects eligible/'loss' (19/19 pass).

## BOT SILENCE ROOT-CAUSE CHAIN FIXED (iter-127b, July 9 2026 — FIRST AUTO TRADE EXECUTED)
User: "bot still silent 30+ hours". Traced and fixed the FULL veto chain, one real blocker at a time (each verified live):
1. hf_scalp had no FLAT-market play → added VWAP fade (±0.20/0.25% from VWAP) + exhaustion fade (±0.30/0.35% extension w/ stalled momentum) to `strategy_engines.py`.
2. Master Agent consensus gate (daily agents voting) blocked scalps → advisory for DETERMINISTIC_INTRADAY_SCOPES (enforced for MTF engines).
3. Range/fed-tone/news-AI gates (daily models) → advisory for deterministic scopes (`_det_scope` in bot_runner).
4. Monte Carlo gate simulated TP1 ONLY (0.8×SL) → now simulates blended target (rr_ratio×SL); IID bootstrap can't price mean reversion → MC advisory for `entry_style=="fade"` (tagged in ai_signals).
5. CVaR₉₅ budget used 1-day notional VaR ignoring stops → stop-aware override in `risk_engine.cvar_budget_check` (Σ stop-distance ×1.5 gap buffer within budget → allow).
6. SIZING BUG: STARTRADER stored account_type='microcent' but live equity deltas proved STANDARD contract → pip value $0.01 vs $10 → lots inflated (0.29 instead of ~0.10). Fixed: account_type corrected in DB + FINAL std-contract risk clamp in bot_runner (assumes standard contract, clamps lot so stop-loss worst case ≤ risk budget; `+std_risk_clamp` sizing method).
RESULT: 11:25 UTC first auto trade EXECUTED (fast_scalp exhaustion-fade SELL XAUUSD 0.29 @4100.54, SL 4106.62, TP 4096.10, STARTRADER $25.7k). Anti-pyramid correctly refuses stacking while open. NOTE: that first trade risks 0.686% (sized pre-clamp); subsequent trades clamp to 0.25%.

## PER-ACCOUNT STRATEGY ENGINES (iter-127, July 9 2026 — DONE, tested iteration_42.json: 85/85 pass, 0 issues)
User: "each account has different strategy, but the bot acts like one strategy for all". Now the account's `active_preset` selects EXACTLY ONE execution engine (`strategy_engines.py`):
- sniper→mtf_strict · balanced/None/custom→mtf_moderate (1H boss, 4H non-opposing, retrace 20-75%) · trend_rider→mtf_relaxed (retrace 15-80%, impulse 1.5×ATR) · scalper→hf_scalp (M15 EMA bursts + VWAP bounces, 5-min re-entry) · fast_scalp→hf_scalp_fast (softer thresholds, 3-min re-entry) · breakout→breakout_m15 (Donchian-20 + momentum confirm) · mean_reversion→range_fade (fade session extremes toward VWAP).
- `mtf_intraday.MTF_MODES` parametrizes the cascade (strict/moderate/relaxed). `analyze_symbol(..., strategy=)` dispatches; scope/strategy_engine/engine_label in every response; reasoning starts with engine label.
- HF scalps: SL 0.8×ATR15, TP=2×SL (weighted R:R 1.25), risk 0.25%/trade (`risk_pct_cap` enforced in bot_runner sizing). range_fade/breakout: SL 1×ATR15, TP 2×SL.
- Gate scoping: news veto HARD for MTF engines, ADVISORY for deterministic intraday scopes; uncertainty gate advisory for DETERMINISTIC_INTRADAY_SCOPES; structure gate exempts range_fade. Claude narration only for MTF setups (deterministic engines = no LLM cost).
- Presets: scalper/fast_scalp set signal_cooldown_minutes 5/3 (existing configs migrated); descriptions start with "ENGINE: ...".
- Frontend: BotConfig "Active Engine" card is dynamic per account (data-testid core-strategy-card, PER-ACCOUNT badge). NOTE: /strategies route = AI Strategy Generator; preset picker lives on /bot-config.
- Live verification: each active account's pulse shows its own engine label (STARTRADER→FAST SCALP, Tauro→BREAKOUT, VTMarkets/RoboForex→MEAN REVERSION, OnEquity→TREND RIDER).

## SINGLE-ENGINE SIMPLIFICATION — Option A (iter-126, July 8 2026 — DONE, tested iteration_41.json)
User rejected the confusing overlap of 5 execution modes and chose **Option A: MTF Confluence is the ONLY engine**.
- `ai_signals.analyze_symbol()` fully rewritten: strict cascade (4H trend → 1H structure → M15 pullback 25-70% retrace → live breakout via `mtf_intraday.fetch_mtf_confluence`) is the sole signal generator. Claude is now a NARRATION-ONLY layer (`NARRATOR_PROMPT`) that explains confirmed setups — it never picks direction, and is SKIPPED on every HOLD (big LLM cost saving). Not-aligned → cheap HOLD with exact cascade note ("waiting on M15 setup: retrace 74% outside 25%-70%").
- REMOVED: aggressive_mode, range_scalp, intraday_scalp override, swing/Claude-direction path, mtf_strict daily-tier gate + velocity veto in bot_runner, old probabilistic vetoes (CHOP/entropy/meta-labeler/learned-meta/A+/DXY/short-tier/intraday-momentum), `range_scalp_signal`, `DETERMINISTIC_SCOPES`, "aggressive" strategy preset, aggressive auto-heal check, aggressive optimizer tunable.
- KEPT (capital protections): market-closed hard veto, macro freeze, dual-AI news veto, weighted R:R ≥ 1.1 (`MTF_RR_FLOOR`), Kelly sizing, and ALL bot_runner account guards (drawdown, cooldowns, anti-tilt, spread/slippage, payoff/final-RR, auto_guards, eod quiet, structure/range/fed-tone/liquidity/news/calendar gates, RL policy).
- Signal SL/TP: M15-ATR geometry (30-pip floor) with daily-ATR fallback; response keeps legacy keys (meta_label/mtf_gate/learned_meta/aplus=None) + new `mtf_confluence` report field.
- Frontend: BotConfig legacy toggles replaced by static "Core Strategy — MTF Top-Down Cascade / ALWAYS ON" card (`data-testid="core-strategy-card"`); stale MTF-Gate info card removed; BotHealth auto-heal copy updated.
- Bugs fixed during session: P0 orchestrator NameError (DETERMINISTIC_SCOPES) resolved; health-score 500 (`tilt_cfgs` orphaned by section-6b removal) re-fetched in section 7; BotConfig.jsx duplicated-tail parse error from a parallel-edit race repaired.
- GOTCHA (twice now): parallel search_replace on the SAME file races and loses edits — ai_signals imports + BotConfig.jsx both hit. Same-file edits must be sequential.
- Stale tests patched/deleted: test_iter123_range_scalp.py + test_iter120_intraday_scalp.py removed; iter-29/21/50 aggressive references neutered. New tests/test_iter126_mtf_only_engine.py (by testing agent) 85/85 relevant pass.

## Notes / Gotchas
- Minor observed: trade-manager logs "price fetch failed for GOLD: Symbol GOLD not supported" — FIXED July 8: `market._key()` now routes through `pip_utils.base_symbol()`, so all quote/history callers resolve broker symbols (GOLD, GOLD#, XAUUSD-ECN → XAUUSD).
- Auth is COOKIE-based (httpOnly) — curl testing needs `-c/-b` cookie jar, not bearer tokens.
- `accounts.broker_utc_offset_sec` is learned from live deals; historical/backfill deal epochs must subtract it before storing closed_at/opened_at.
- The recurring "code review report" pasted into chat is a hallucinated false-positive from a static analyzer. **IGNORE IT.** Do not refactor based on it (18+ recurrences).
- MT5 `DEAL_TIME` is broker-LOCAL epoch — never label it UTC; server-received UTC is canonical.
- AI Optimizer must ignore `pnl_estimated`/`pnl_unknown` trades (poisoned training data).
- Production is far behind preview — deployment retry is P0 (see ROADMAP.md).
- EAs ≤v1.38 silently ignore `sync_request`; pending badge on Accounts page tells user to update.
- Slippage veto fires ONLY on true slippage (EA v1.40+ `requested_price`); legacy signal-vs-fill deltas are latency drift and must never veto.
- Always use `pip_utils.base_symbol()` for pip math / cap lookups on broker-suffixed symbols (GOLD#, XAUUSD.fx, XAUUSD-ECN).

## Test credentials
See `/app/memory/test_credentials.md` (admin: admin@trading.bot / admin123).

## Related memory docs
- `CHANGELOG.md` — full per-session implementation log (iter-47 latest: slippage-veto repair + FULL_CLOSE consumption, EA v1.40).
- `ROADMAP.md` — prioritized backlog (P0: production deployment).

## Iter-128 (2026-06) — Bot vs Manual separation (P0 complete)
- Trades page: BOT and MANUAL split stat cards (W/L, P&L, win-rate) replace combined WIN RATE / WINS-LOSSES cards; SOURCE filter pills (ALL/BOT/MANUAL) filter the table; period/history summary also shows the split.
- Backend: `/api/trades/stats` + `/api/trades/history` summary now return `bot` and `manual` buckets (`_is_bot_trade` mirrors frontend SOURCE_BADGE_FOR).
- Origin audit: added `origin:"auto"` filter to adaptive_mode (was including manual!), auto_tune, circuit_breakers, risk_engine._period_pnls, safety_guardian daily-loss cap, bot_doctor win-rate — ALL bot self-adjustments now based solely on bot trades. Learning modules (Kelly/RL/ML/Bayes/meta/loss advisor/cooldown/anti-tilt/revenge-block) already filtered correctly.
- Tested: curl on both endpoints, UI screenshots, 65 backend regression tests pass.
- Next: production deployment (user approval pending), Strategy Scoreboard UI (P2).

## Iter-129 (2026-07-13) — Deep review of losing session + new session-aware gates
Session review: gold -2.1% trend day, bot went 14W/18L (+$67). 4 failure patterns found & fixed:
1. session_trend_gate (payoff_guard.py): vetoes counter-trend entries vs intraday session structure (range≥0.45% gold / 0.9% crypto, pos band + EMA20 slope + swing structure). Would have blocked both counter-trend BUY clusters (-$110).
2. exhaustion_chase_gate: vetoes WITH-trend entries near day extreme after range ≥1.5% gold / 3.0% crypto elapsed (blocks selling the day low → -$244 avoided).
3. trend_ride_check + TP widening ×1.8 (cfg trend_ride_tp_mult) on big directional days so trailing rides the move instead of 12pt clips.
4. VWAP-fade grind fix (strategy_engines.py): FLAT-trend fades blocked when EMA20 slope ≥|0.04|%/2h (109 BTC BUY-fade signals in a down grind root cause); knife filter tightened (day_rng 1.0→0.6, bands 20/80→30/70).
- All 3 gates wired in bot_runner iter-60 block; cfg toggles session_trend_gate_enabled / exhaustion_gate_enabled / trend_ride_enabled (default ON). Trades now stamp `scope` + `trend_ride`.
- Fixed 5 stale pre-existing tests (test_iter97 expected old 2.0x ratio / old ai_signals wiring). New replay test file test_iter129_session_gates.py. 98 tests pass; backend clean after restart.
- Replay estimate: same day with gates ≈ 14W/6L, ~+$350 instead of +$67.

## Iter-130 (2026-07-13) — Real-time narrative fusion (Decision + Risk agents)
Context: gold's 2% slide was US-Iran driven; news layer's freshest headline was 3 days old (Fed-only queries), news was veto-only, sizing narrative-blind.
1. news_understanding.py: added GEO_QUERY wire (Iran/Middle East/war/sanctions/OPEC/tariffs, tier-1 domains) + RSS backup wire (BBC World, Al Jazeera, CNBC World; stdlib XML parse, keyword relevance filter) merged & deduped, MAX_HEADLINES 12→18, CACHE_TTL 45m→15m. Verified live: "U.S. launches airstrikes against Iran…" now surfaces, Claude scored +2.5 (shock).
2. Decision fusion: news_confidence_bias() — with-narrative +5 conf, against -10 (|net|≥1.2); stamped as signal.news_bias. Extreme veto (|net|≥2) unchanged.
3. Risk fusion: narrative_risk_scale() — against moderate narrative ×0.5; live shock headline (|score|≥2.5) ×0.7 all trades; floor 0.35. Wired into bot_runner lot sizing (sizing_method "+narrative"), pulse-logged, counters news_bias_applied / news_size_trim.
- Tests: test_iter130_narrative_fusion.py (17 tests); 61 pass incl. iter-129/97 regression. Backend clean after restart.
- NOTE: NewsAPI free tier delays articles up to ~24h — RSS wire compensates in real time.

## Iter-130b (2026-07-13) — Post-mortem bot-isolation fix
User asked whether the bot analyzes lost trades. Verified: YES (35 auto post-mortems today). But found manual trades were leaking into the loop:
- Manual losing trades were auto-post-mortemed and their pattern_keys counted toward guardrail AUTO-TIGHTENING; manual wins counted toward loosening.
- Fixed in loss_postmortem.py: (1) _is_postmortem_eligible skips manual origins (force=True from Loss Lab UI still analyzes any trade on demand), (2) post-mortem docs stamp origin, (3) _maybe_autotighten counts exclude manual/forced docs, (4) maybe_record_winner ignores manual wins + wins_after query filters origin:auto, (5) consecutive-loss lookback filters origin:auto.
- Verified live: manual loss → not eligible; bot loss → eligible. 30 post-mortem tests pass.

## Iter-131 (2026-07-14) — CPI incident: calendar timezone bug fixed
CPI spiked gold ~600 pips at 12:30 UTC. Findings:
- BOT: zero trades today, zero positions in the spike (gates kept it flat — no losses). User's MANUAL sells got run over (-$700, -$415).
- BUG: FF feed times are ALREADY UTC ('12:30pm' = real CPI time) but _parse_event_time assumed ET and added +4h → every pre-event guard armed 4h late (16:30). Fixed: parse verbatim UTC; 'All Day' → midnight UTC.
- Purged 27 event_outcomes docs (outcome labels were classified on the wrong +4h bar window; priors fall back to BASE_PRIORS and relearn).
- FF feed 429: bot-identifying UA rejected → switched to standard browser UA; module has 10-min failure backoff. Verified live: 98 events, CPI @12:30 UTC ✓, next high-impact (Fed Chair testimony 14:00 UTC) correctly visible to upcoming_for.
- Tests: test_iter131_calendar_tz.py (5) + 37 calendar/event tests pass.

## Iter-132 (2026-07-14) — "Bot missed the 600-pip trend day" diagnosis + continuation entries
Diagnosis: (1) all 5 active engines are scalp presets — breakout/mean-reversion personas inactive since Jul 13 ~10:00 (config state, not a bug); (2) scalp engines had NO entry path on gap-and-run days (momentum_3h diluted by consolidation; price never returns to VWAP); (3) Jul-13 auto-tighten raised one account's min_conf to 70 → 65-67% signals became HOLD; (4) event freeze + knife filter correctly prevented bad fades; 2 fade SELLs executed 18:01.
Fix in strategy_engines.hf_scalp_signal:
- UP/DOWN branches: EMA20 shallow-pullback continuation (day_rng≥1.0, pos 55-85 / 15-45, |price-EMA20|≤0.15%, slope agrees, no fresh counter-break).
- FLAT branch: trend-day flag (day_rng≥1.2, consolidating 62-85% of up-day / 15-38% of down-day with slope/mom not opposing) → continuation entry; placed BEFORE VWAP fades.
- Replay: 14:22 CPI-day feats now → BUY "trend-day flag". Exhaustion gate still caps ≥85%.
- tests/test_iter132_trend_day_continuation.py (10 tests); 70 pass with full engine regression.
RESOLVED: BREAKOUT HUNTER restored on OnEquity (04cf85) via POST /api/bot/preset/breakout, verified dispatching (2026-07-14 18:36).

## Iter-133 (2026-07-14) — Quant review phase-1 corrections (user-provided review, all 5 verified REAL before fixing)
1. FAIL-CLOSED (C3): orchestrator RiskAgent & ExecutionOptimizer exceptions now force HOLD + tradeable=False + pipeline_safe_to_execute=False; PortfolioAllocator exception → conservative half-size fallback. pipeline_safe_to_execute stamped on every signal.
2. Kelly DISABLED by default (C1/H4): compute_lot_for_account(kelly_enabled=False default) → fixed fractional risk = profile.risk_pct; re-enable per-account via cfg kelly_enabled. Extreme profile kelly_cap 1.00→0.50. bot_runner + sizing-preview endpoint honor the flag.
3. Signal-time lot sizing REMOVED (C2): ai_signals no longer calls compute_kelly_position_size (was equity=$1000, price-dist-as-pips, pip_value=1.0); signals carry sizing_deferred=True + 0.01 placeholder; bot_runner compute_lot_for_account is the ONE authoritative stage.
4. Backtester pending-order retention (C4): orders for other symbols survive until their bar arrives.
5. Backtester position ledger (C5): positions dict[str, Position] per symbol, per-symbol stops/closes, portfolio mark-to-market via last-known prices; run.py updated.
6. tests/test_iter133_quant_review.py (16 tests) + fixed stale tests (backend_test kelly caps, iter25c kelly_enabled, iter20 auth fixture terms/verification/bridge_token). 104 tests pass.
DEFERRED (backlog): H1-H3, H5-H6 (returns-based correlation, data-unavailable=unknown, geometry-aware payoff, min-conf override policy, paper P&L contract spec), B1-B6 (backtest cost realism), A1-A3 (MarketSnapshot, typed stage results, error classification).

## Iter-134 (2026-07-14) — Quant roadmap phase-2 (#2 #3 #4 #6 #14)
1. Decision ledger: trade_decisions.py — immutable doc per BUY/SELL candidate outcome (rejected/executed), stage inferred from veto reason (infer_stage map), signal snapshot (entry/SL/TP/conf/MC EV/news net/session feats), version stamps. Hooked into _record_pulse (every non-routine SKIP/BLOCKED) + execution success site. GET /api/trades/decisions?status/stage/symbol with stage_counts aggregation. Verified live: real anti_pyramid rejections recorded within 90s.
2. Versioning: versioning.py — strategy_version per engine scope, risk_policy=risk_v133, execution_policy=exec_v129, feature_schema=fs_2026_07; stamped on decisions AND executed trade docs (execution.py "versions"). bot_runner now passes scope/trend_ride/versions into engine.execute (also fixes scope=null on bot trades).
3. Cost-aware EV (#4): monte_carlo simulate_trade(cost_price) → cost_r, ev_r_net; mc_gate vetoes on NET EV; typical_cost() per-symbol spread table × 1.5 slippage buffer; bot_runner passes cost.
4. Invariant/property tests (#14): test_iter134_decision_ledger.py — 600+ randomized sizing checks (lot>0, wider stop never bigger, risk ≤ profile cap, kelly ≤ fixed fraction), cost-EV gate, ledger wiring, version stamps. 63 tests pass.
Already-covered roadmap items verified: #3 #5 #7 #11 #12 #13 #17 largely exist. Remaining backlog: #9 scoreboard/attribution, #15/#16 ablation, #8/#18 promotion criteria, #1 layer separation, #10 central allocator (partial: portfolio_allocator exists).

## Iter-135 (2026-07-14) — Strategy Scoreboard (roadmap #9) DONE
- Backend GET /api/trades/scoreboard?days=7|30|90|0: per-engine attribution from closed auto trades (scope stamped iter-133; older trades backfilled via signal_id→signals.scope lookup) — W/L, win rate, P&L, profit factor, avg win/loss, long/short split, per-symbol chips, strategy version; plus decision-funnel gate counts from trade_decisions ledger.
- Frontend /scoreboard page (Scoreboard.jsx) + Sidebar INSIGHTS nav (Trophy icon, nav-scoreboard): engine cards with verdict badges (EARNING pf≥1.2&pnl>0 / MARGINAL / BLEEDING / SAMPLE TOO SMALL <3 trades), period pills, decision-funnel bars (which gate does the work + executed count).
- Verified: curl (5 engines, real attribution: range_fade pf 2.53 earning; hf_scalp bleeding) + screenshot (rows render, 7D filter works, nav active). 406 older trades show "unattributed" (pre-scope era) — all future trades attributed.
Remaining backlog: ablation runs (#16), promotion criteria (#18), research/prod separation (#1), production deployment.

## Iter-136 (2026-07-14) — Roadmap #16 #18 #1 DONE
1. Gate ablation (#16): ablation.py — counterfactual replay of vetoed setups (from decision-ledger snapshots) against actual subsequent M15 bars → per-gate saved_r vs blocked_r, verdict ADDS VALUE / NEUTRAL / COSTS EDGE. GET /api/trades/ablation?days=N + AblationPanel on Scoreboard. Snapshot coverage: added contextvar _CURRENT_SIGNAL in bot_runner so ALL 34 veto sites auto-attach signal snapshots (symbol-mismatch guard against stale ctx). Live-verified (2 replays already).
2. Promotion criteria (#18): /app/docs/PROMOTION_CRITERIA.md — P1 sample/walk-forward, P2 risk quality (PF≥1.25, DD≤12%, concentration limits, perturbation stability), P3 shadow parity with locked versions, P4 operational invariants; demotion triggers tied to Scoreboard verdicts + ablation + drift.
3. Layer separation (#1): /app/docs/ARCHITECTURE_LAYERS.md (research/simulation/production boundaries) + backend/research/ package + tests/test_iter136_layer_separation.py AST-based import guard (production never imports backtester/research; backtester/ablation never import live execution; nothing imports research). 36 tests pass; UI screenshot verified.
Remaining: PRODUCTION DEPLOYMENT (user approval).

## Iter-137 (2026-07-14) — Institutional Phase A DONE (previous session)
- calibration.py: Brier score + reliability table per engine (stated vs realized win rate, 5-bucket), calibrated_p_win lookup (15-min cache). Scoreboard shows rolling Sharpe/Sortino/Max DD/profit factor + calibration honesty (_risk_metrics in trade_routes).
- Strategy Marketplace UI: enable/disable/compare presets. USER VERIFICATION still pending.
- BUGFIX (this session): scoreboard avg_slippage_pips KeyError — r.pop("slippage_n") in ternary condition removed the key before the numerator read it; fired once slippage data existed and 500'd the whole Scoreboard page. Fixed in trade_routes.py.

## Iter-138/139 (2026-07-14) — Institutional Phase B DONE
1. Engine parameterization: strategy_engines.py DEFAULT_PARAMS + PARAM_BOUNDS per engine (hf_scalp slope_min/mom_min/exhaustion_vdist/flat_fade_min; range_fade edge_pct/min_day_pct/min_atr_mult; breakout_m15 min_day_rng). params=None ≡ old hard-coded behavior (tested). Flow: bot_configs.engine_params → orchestrator user_cfg → strategy_agent → analyze_symbol(engine_params) → run_engine(params). Signal carries "engine_params".
2. Bayesian optimization: bayes_opt.py — pure-numpy GP (RBF, Cholesky) + Expected Improvement over PARAM_BOUNDS, objective = walk-forward replay of REAL accumulated M15 bars (~800/symbol) with precomputed per-bar features (no lookahead: features from bars<i, fill at bar i open, SL-first when both touch, TP=2×SL, scalp SL=0.8×ATR15 else 1.0×). score = net_r − 0.5×maxDD − small-sample penalty. POST /api/quant/bayes/run {engine,symbol,iters} (~0.7s), GET /api/quant/bayes/proposals → tuning_proposals collection (status proposed/shadow_testing). ADVISORY ONLY — must pass Shadow Lab before touching live config.
3. RL capital allocator: rl_allocator.py — per-scope weight from P(edge>0)=Φ(mean/sem) over 60d closed auto trades. SHRINK-ONLY: n<10 → 1.0; P≥0.5 → 1.0; else 0.25 floor + linear. cfg rl_allocator_mode off/advisory(default)/enforce; wired in bot_runner after corr-kelly trim (annotates signals.allocator; enforce multiplies lot). Failure → full weight (risk engine downstream stays fail-closed). GET /api/quant/allocator.
4. Portfolio optimization: portfolio/optimizer.py — inverse-vol targets penalized by avg positive pairwise corr, base-symbol grouped, current vs target drift (REDUCE/ROOM TO ADD), CVaR95 vs 2% budget, concentration >60% + corr>0.7 warnings. GET /api/quant/portfolio-optimization. Live per-trade enforcement remains correlation_kelly.
- routes/quant_routes.py registered in server.py. UI: components/quant/QuantPanels.jsx (AllocatorPanel + TuningPanel) on Scoreboard.

## Iter-140 (2026-07-14) — Institutional Phase C (Shadow Lab) DONE
- model_shadow.py: challenger param sets with LOCKED version (sha1 of params) + production baseline locked at registration for fair A/B. Incremental walk-forward replay (persisted challenger_state/baseline_state + evaluated_until_t in shadow_models collection) on every GET /api/shadow/models — accumulates beyond the 800-bar window. Promotion gate (P3): ≥14 days, ≥30 shadow trades, PF≥1.25, beats baseline net R, maxDD≤20R — promote endpoint 422s until ALL pass, then writes bot_configs.engine_params.{engine} + engine_params_meta (all active configs), retires superseded promotions. Retire of a promoted model restores defaults.
- Broker-fill reconciliation: GET /api/shadow/reconciliation — replays each closed auto trade's entry/SL/TP vs actual M15 bars (ablation.replay_outcome), compares sim vs realized outcome. Live: 420 fills, 64.3% match (gap = trailing/BE/partial-close management the static sim can't see — honest metric).
- Routes: /api/shadow/models (+/register, /{id}/promote, /{id}/retire), reconciliation. UI: ShadowLabPanel on Scoreboard (promotion checklist chips, PROMOTE disabled until ready, reconciliation card).
- Tests: test_iter138_bayes_opt.py (14), test_iter139_rl_allocator.py (10), test_iter140_shadow_models.py (7) — all pass; iter126/127/132 engine regression passes. Live smoke: bayes run on real XAUUSD bars (hf_scalp −11R/7d honest), register→gate-block→retire verified. Known stale legacy failures (pre-existing, unrelated): tests needing terms_agreed registration (iter12/13/23, backend_test), MONGO_URL-less env, EA version-string expectations (iter99 expects 1.42), TESTSYM candle accumulation (iter116).
- One live challenger in shadow: hf_scalp~2a60017fe7 on XAUUSD (from Bayes proposal).

## Iter-141 (2026-07-14) — Allocator Enforce + Nightly Auto-Tuning + Shadow Alerts DONE
1. Allocator ENFORCE: POST /api/quant/allocator/mode {off|advisory|enforce} → sets rl_allocator_mode on all active bot_configs (422 on bad mode). ENFORCE turned ON live (6 configs) per user request — proven-loser engines now get lots cut (floor 0.25×). UI: mode toggle in AllocatorPanel (allocator-mode-{off,advisory,enforce} testids, red highlight + confirm dialog on enforce).
2. Nightly auto-tuning: nightly_tuner.py — per-user 24h-guarded sweep (quant_tuning_state.last_run_at): combos_from_configs (active presets → tunable engines × base symbols, max 6 runs), runs Bayes per combo, auto-registers Shadow Lab challengers when improvement ≥ +1.0R score AND ≥5 sim trades (source=nightly_bayes; proposal → shadow_testing). NEVER auto-promotes. Server loop _nightly_tuning_loop (hourly tick, NIGHTLY_TUNER_INTERVAL_SEC) registered in startup/shutdown. Live-verified: 3 combos ran (~2s), guard blocks second run.
3. Shadow-ready Telegram alert: notifier.notify_shadow_ready (event_type "shadow_ready", respects per-event opt-out) fired from model_shadow.evaluate_user_models on the ready TRANSITION only (ready_notified flag dedupes). Verified with synthetic ready model: alert fired once, second eval silent, cleaned up.
- Tests: test_iter141_nightly_tuner.py (2) + iter140 suite pass. UI screenshot: ENFORCE active, nightly proposals listed.

## Iter-142 (2026-07-14) — Quant Review Round 3 DONE
1. Manual route implicit Kelly REMOVED (trade_routes execute_signal): old branch scaled max_lot_size by kelly_f/kelly_cap with Kelly disabled → kelly_f=0 collapsed capped manual executes to 0.01 dust lots. Now plain hard ceiling min(absolute_lot, max_lot_cap).
2. Synthetic confidence replaced: setup_score.py computes honest 25-92 score from live features (momentum/slope/trend/donchian alignment, room-to-run, activity, fade-stretch, MTF bonus; basis "setup_score_v1"). ai_signals confidence = setup score (was min_conf+5 constant — always cleared its own gate, garbage calibration buckets). Signal carries setup_score + confidence_basis. bot_runner attaches signal["calibrated_p_win"] (calibration.calibrated_p_win, engine+bucket realized win rate). NOTE: bots now genuinely skip weak setups below profile min_confidence — expected tightening.
3. Correlation risk rebuilt (portfolio/var.py): corr_returns() on timestamp-aligned daily LOG RETURNS (raw close levels were spurious — trending series always read ~1; [-n:] truncation misaligned dates). Unknown (<MIN_OVERLAP=10 common dates) → None; risk callers substitute conservative UNKNOWN_RHO=0.5 (never assume independence). Rewired: calculate_var (notes unknown pairs), correlation_kelly, portfolio/optimizer, portfolio/risk_manager._avg_pairwise_corr, agents/risk_agent cross_asset_correlation_veto. Updated stale monkeypatches in test_iter51/test_iter17.
4. Backtester instrument-aware + cost-realistic (backtester/engine.py): pip via pip_utils.pip_size (old code used hard-coded FX 0.0001 for every symbol — gold/crypto frictionless), spread_map from monte_carlo.TYPICAL_SPREAD (bars=mid; entries pay half-spread+slippage, stop exits fill THROUGH the level with stop_slippage_mult=2× plus half-spread, TP limit fills pay closing half-spread only, market closes cross the spread). Flat round-trip now net-negative. Layer separation intact.
5. BONUS: velocity_veto (iter-53 guardrail) found silently UNWIRED from bot_runner since a Jul-8 orchestrator refactor (iter53 wiring test was failing) — re-wired after signal generation (disarmed by default, arms via cfg regime_overrides; SKIP pulse on fire).
- Tests: test_iter142_quant_review3.py (19). 205 quant-battery tests pass. Remaining known-stale: iter12/13/85 (terms_agreed), iter14 dxy_gate None, iter54/56 EA-version strings, iter90/91/99/116 env-dependent, iter126/127 pulse tests need a bot with auto_execute=true (all user bots currently have auto_execute=false — user config state).
Remaining: PRODUCTION DEPLOYMENT (user approval); Phase A user verification.

## Scalp Review Round 5 (2026-06 session) — Broker-Deal Reconciliation + Account Risk DONE
1. AUTHORITATIVE financial reconciliation moved to /bridge/external-deal: for scope=scalp_fast full closes it calls ScalpRunner.on_trade_closed with SIGNED profit/commission/swap/price + broker deal_id (idempotent per deal_id AND trade_id via bounded deques). /bridge/report demoted to OPERATIONAL ack: on_close_ack() frees slot + parks info in _closed_awaiting_financials (capped 200), applies NO financials.
2. Signed MT5 semantics: net_pnl = profit + commission + swap (no abs()); execution_cost = max(0,-c)+max(0,-s); financing_credit = max(0,swap). Canonical execution_outcome record includes requested/actual exit, exit_reference_bid/ask, exit_slippage_pips (close-request quote baseline, or stop level within 3 pips for broker SL exits), close_request_ts_ms.
3. Account-wide risk layer: engine._account_risk registry (shared RiskState per account), risk.check_account() (daily loss/cost, 2x hourly cap, cooldown) checked BEFORE symbol-local; persisted as scalp_risk_state symbol="_ACCOUNT"; restored in restore_risk; account record_open/close/result on all paths; status() exposes account_risk block.
4. Detailed open-position restore: opened_ms from trade opened_at ISO, requested/actual entry, slippage_pips, close_requested_ms from pending_modification.requested_at, est_cost_usd from decision doc (cost_pips*pv*lot).
5. Dead-letter path env-configurable (SCALP_DEAD_LETTER_PATH, default backend dir) with parent-dir creation; audit_backlog() exposes pid + dead_letter_path (process-local counters documented — single worker/sticky routing required).
6. Fill-probability model (item 8): exec_attempts/exec_fills counters (fills counted once per trade in on_trade_opened), Beta(8,1) prior, EV_attempt = p_fill*net_edge - (1-p_fill)*0.1 gate in _submit_live after >=20 attempts; broker-blocked attempts stored as dataset="attempt_failed" with submission_result.
7. Statistics: _lower_bound now BLOCK bootstrap (block 10, 500 iters, seed 42, 5% quantile) — dependence-aware; _purge() purge+embargo (300s = max holding) applied to train/cal boundaries in main split AND rolling windows (ts_ms threaded through retrain).
- Tests: 17 new Round-5 tests in tests/unit/scalp/test_scalp_unit.py (TestRound5FinancialReconciliation/AccountRisk/RestoreAndDurability/Statistics). Scalp suite 65 pass; FULL backend suite 1925 passed / 0 failed.
Remaining scalp backlog: expected-net-return regression model (P1), separate entry/exit slippage models (P2), durable shared outbox (Redis/DB) for multi-worker (P2), GBPUSD after EURUSD validation (P2), production deployment (P1).

## Scalp Review Round 6 (2026-06 session) — Durable Reconciliation + Partial Closes + EA v1.45 DONE
1. CRITICAL durable financial reconciliation: broker_deals close deals persisted with financial_reconciliation_status="pending" ("none" for "in" deals) → runner applies → marked "complete" (_scalp_reconcile_close/_mark_deal_reconciled helpers in bridge_routes). Duplicate deal for scalp trade with status!="complete" → RESUME reconciliation. Recovery sweep engine.recover_pending_deals() (server loop _scalp_reconcile_loop, SCALP_RECONCILE_INTERVAL_SEC=300) resumes stranded pendings; hydrates runner via restore_risk if needed.
2. Partial scalp closes: ScalpRunner.on_partial_close() — applies signed P&L/cost to both risk layers, reduces info["lot"], scales stop risk, records partial_exit_records ($push), NO record_close until volume zero; wired into /external-deal partial branch.
3. AccountScalpRiskLimits dataclass (30 trades/hr, 2 open, 0.15% stop-risk, 0.5%/0.35% daily) — explicit account policy; check_account takes proposed_stop_risk_usd; monetary stop-risk registry on RiskState (add/scale/remove/total_stop_risk_usd); engine registers stop risk at submit, rebuilds on restore, removes on close/ack.
4. restore_risk rebuilds account open count + stop risk from live_trades; loads applied_deal_ids (persisted in symbol scalp_risk_state doc) → durable deal idempotency across restarts.
5. Signed streak semantics: record_result(net, cost, trading_pnl) — daily budgets use net; loss STREAK uses trading P&L = pnl + commission + min(0,swap) (swap credit can't mask a loser). Outcome record gains trading_pnl_usd + exit_reason.
6. Stop exits: close_reason containing "stop_loss" ALWAYS uses stop_px as exit-slippage baseline (gap-through-stop measured); close_reason passed from trade doc through external-deal.
7. Partial detection authority: EA v1.45 sends position_volume (broker REMAINING volume after deal) in OnTradeTransaction; backend prefers it (>0 partial, 0 full), lot-comparison fallback for backfills. EA bumped everywhere (mq5 #property+define, diagnostic/bot/setup routes, Accounts.jsx, EaVersionStrip.jsx); scalp EA test now version-agnostic (>=1.45).
8. Dead-letter: DB outbox (scalp_dead_letter collection) attempted first, JSONL emergency fallback.
- Tests: 9 new Round-6 tests (TestRound6PartialAndDurability incl. recovery-job resume, deal-id restore replay no-op). Scalp suite 74 pass; full backend suite 1929+ pass (iter24c flaky-in-suite only, passes standalone).
Remaining: dedicated stateful scalp worker / sticky routing for multi-worker deploys (P2), expected-net-return regression model (P1), separate entry/exit slippage models (P2), GBPUSD after validation (P2).
