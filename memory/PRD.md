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

## Notes / Gotchas
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
