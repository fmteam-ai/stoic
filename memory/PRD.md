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
