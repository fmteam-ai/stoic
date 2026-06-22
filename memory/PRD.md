# AI Trading Bot — Product Requirements Document

## Original Problem Statement
> I want to build an AI trading bot. The bot will have 4 options for risk management: low, middle, high, and extremely high. The bot will analyze market conditions and the past 6 months in real time. The bot will trade gold (XAUUSD) and bitcoin (BTCUSD), and will support adding more symbols to trade.

## User Clarifications (gathered during ask_human)
- **Live trading** via MetaTrader 5 micro-cent accounts (bridge architecture).
- **AI engine**: Claude Sonnet 4.5 (anthropic, claude-sonnet-4-5-20250929) via Emergent Universal LLM key.
- **Market data**: started with Alpha Vantage; replaced with multi-source free providers (CoinGecko, gold-api.com, open.er-api.com, Frankfurter) due to AV rate limits and lack of XAU support.
- **Auth**: JWT email/password (httpOnly cookies).
- **Multi-account**: users can connect several MT5 accounts simultaneously.
- **Mobile**: responsive web app, accessible from any Android browser; can be added to home screen.

## Architecture
- **Backend**: FastAPI (Python) + Motor (MongoDB async)
- **Frontend**: React + Tailwind + Recharts + lucide-react
- **AI**: Claude Sonnet 4.5 via `emergentintegrations.llm.chat.LlmChat`
- **Live trading**: MT5 bridge → downloadable MQL5 Expert Advisor (`/api/ea-script`) polls our server using a per-account `bridge_token` and executes trades on the user's Windows MT5 terminal.

## Personas
- **Active retail trader**: wants AI co-pilot for gold + crypto, runs MT5 on a Windows VPS.
- **Algo-curious newcomer**: starts on a micro-cent demo account, tweaks risk profiles.
- **Multi-account operator**: runs the same strategy across several broker accounts.

## Core Requirements (static)
1. 4 risk profiles: low / medium / high / extreme (lot sizing, SL/TP multipliers, confidence threshold, leverage cap).
2. Real-time live quotes + 6-month daily history for XAUUSD and BTCUSD; symbol set extensible.
3. AI generates BUY/SELL/HOLD signals with reasoning, entry, SL, TP and confidence.
4. Live execution on MT5 via downloadable Expert Advisor + bridge tokens.
5. Multi-account support (each account = its own bridge token + EA instance).
6. Trade history with P&L, win-rate, open/closed/failed status.
7. JWT auth, multi-user.
8. Mobile-responsive UI.

## What's Implemented (Feb 21, 2026)

### Backend
- `auth.py` — bcrypt + JWT (access + refresh, httpOnly cookies, SameSite=None/Secure), `get_current_user`.
- `routes/auth_routes.py` — register / login / logout / me / refresh.
- `routes/market_routes.py` — quote, batch quotes, history, supported symbols, risk profiles.
- `routes/bot_routes.py` — bot config CRUD + start/stop.
- `routes/signal_routes.py` — generate (per-symbol), generate-all, list, delete.
- `routes/account_routes.py` — MT5 account CRUD + bridge token rotation.
- `routes/trade_routes.py` — execute signal → pending trade, close, stats.
- `routes/bridge_routes.py` — EA-only endpoints (heartbeat, poll-trades, report) authenticated via `bridge_token`.
- `market.py` — multi-source data: **CoinGecko** (crypto), **gold-api.com** (XAU), **open.er-api.com + Frankfurter** (FX). Indicators (SMA 20/50/200, EMA 12/26, RSI 14, 6M return, 30D vol).
- `ai_signals.py` — Claude Sonnet 4.5 strict-JSON signal prompt.
- `risk.py` — 4 risk profiles + lot sizing + SL/TP derivation.
- `static/EmergentTradingBridge.mq5` — downloadable Expert Advisor (polls server every 5 s, sends heartbeats, executes BUY/SELL, closes positions, reports P&L).
- `seed.py` — admin seed + indexes.

### Frontend
- Login + Register pages (matching dark "terminal" aesthetic).
- Dashboard: live price tiles (XAUUSD, BTCUSD), 6-month area chart, 8 technical indicators, portfolio stats.
- AI Signals page: generate signals, confidence bar vs. threshold, full reasoning + key factors, execute-on-account dropdown.
- Bot Configuration: 4 risk profile cards (with risk %, confidence ≥, max concurrent, leverage cap), symbol add/remove, max concurrent trades, auto-execute toggle.
- MT5 Accounts: add multiple accounts, bridge token display + copy + rotate, EA download button, live connection status from EA heartbeats.
- Trades: full history table (symbol/side/lots/entry/SL/TP/exit/PnL/status), status filters, manual close, stats tiles.
- Symbols: browse all supported instruments with live quote.
- Mobile responsive (sidebar collapses to top nav under md breakpoint).

### Testing
- Iteration 1: 17/22 passing — Alpha Vantage XAU + rate limit failures.
- Iteration 2: 26/26 backend pass, all frontend flows green. End-to-end trade-bridge flow verified.

## Backlog (next sessions)
- **P1** — Binance live BTC execution via CCXT (blocked: needs user Binance API keys).
- **P1** — Web push notifications (mobile/desktop) on circuit breakers + high-confidence signals.
- **P2** — Server-side slippage veto: compare EA-reported entry vs. signal entry, auto-close if delta > X pips.
- **P2** — Backtest engine — replay 6-month history against the AI signal generator.
- **P2** — Per-profile Meta-Labeler threshold tuning (currently fixed at 0.55).
- **P2** — TTL/retention policy on price_ticks time-series collection (currently auto-purges at 7d).
- **P2** — Add `'paper'` to AccountCreate.account_type Literal (or alias server-side) — currently must pass `account_type='demo' + mode='paper'`.
- **P2** — Frontend WS retry: ensure access_token cookie/query param reattached on every reconnect.
- **P3** — Native Android shell via Capacitor / React Native (PWA already works).
- **P3** — True Mamba SSM inference (requires GPU host) — replace current numpy O(N) compressor.

## CHANGELOG · Feb 22, 2026 — AI Co-Pilot (Live Help Agent)
- **New: In-app AI Co-Pilot** (`copilot.py` + `routes/copilot_routes.py` + `CoPilotWidget.jsx`) — floating chat widget on every authenticated page. Grounded in the user's live state: bot config, accounts, last 5 signals, open + recent trades, panic state, active triggers. Each request fresh-snapshots MongoDB and stuffs the JSON into Claude's system prompt.
- **Multi-turn sessions** persisted in `db.copilot_sessions` keyed by (user_id, session_id). Frontend caches session_id in localStorage so conversation resumes across page reloads.
- **Endpoints**: `POST /api/copilot/chat`, `GET /api/copilot/sessions`, `GET /api/copilot/sessions/{id}`.
- **Hardening**: per-user sliding-window rate limit (30 chats / 5 min), sanitised 502 error message so upstream exception strings can't leak, full server-side exception logging.
- **Frontend**: `<CoPilotWidget />` mounted in `AppLayout` — floating bottom-24 right-6 launcher with pulsing badge, 600px panel with empty-state greeting + 5 quick actions, auto-scroll, Enter-to-send, NEW session reset, X close.
- **Tests**: 117/117 backend pytest (9 new in TestCoPilot — auth-guard, validation 400s, basic chat, multi-turn continuity, list sessions, 404, grounding via /signals/generate, route-conflict regression). Frontend: 100% on widget flows incl. session persistence across reload.

## CHANGELOG · Feb 22, 2026 — Code Review Triage
- **Fixed**: WebSocket reconnect race in `useLiveStream.js` (added `closedRef` guard + retry timer cleanup + onclose null-out on teardown).
- **Fixed**: silent catches in `useLiveStream.js` and `AuthContext.jsx` now log via `console.warn`.
- **Fixed**: array-index keys → stable content-based keys in Dashboard.jsx (macro calendar + key drivers) and Signals.jsx (key factors).
- **Cleaned**: unused imports in `execution.py`, `nl_commander.py`, `models.py`, `server.py`.
- **Declined (with reasoning)**: cyclomatic-complexity refactors of working, fully-tested code; `is None/True/False` "fixes" in tests (PEP-8 recommends them); append-only chat index keys.

## CHANGELOG · Feb 22, 2026 — 2026 Architecture Upgrade
- **Fixed P0**: `entropy_veto is not defined` NameError in `ai_signals.analyze_symbol` — entropy veto block was missing.
- **New: Multi-Engine Consensus + Meta-Labeler** (`meta_labeler.py`) — third-tier classifier on top of Quant + Semantic engines outputs p_true ∈ [0,1] + verdict (TRUE_SIGNAL / FAKE_OUT / NEUTRAL). FAKE_OUT vetoes execution. Logistic regression over 8 explainable features.
- **New: Real-Time Regime Swapping** (`regime_adapter.py`) — DEFENSIVE_SCALP vs. DYNAMIC_MOMENTUM modes auto-toggled by live regime; SL/TP/Kelly cap mutate multiplicatively per regime.
- **New: NL Risk Commander** (`nl_commander.py` + `routes/nl_routes.py` + `trigger_sweeper.py`) — Claude-powered chat translates "if BTC drops 3% disable my high-risk bots" → structured actions executed on the running bot thread. Supports conditional triggers persisted in `db.conditional_triggers`, swept each bot_runner tick.
- **New: NL Strategy Builder** — Claude compiles user prose into a bot_config JSON, previewable + apply-on-confirm.
- **New: O(N) Feature Compressor** (`feature_compressor.py`) — numpy-based Mamba/SSM substitute extracts ~25 stats (trend slope, multi-horizon momentum, ACF, spectral bands, drawdown, skew/kurtosis) from 1-year history in <10 ms.
- **New: MongoDB Time-Series Collections** — `price_ticks` (granularity=seconds, TTL=7d) and `signal_history` (granularity=minutes, TTL=90d). TimescaleDB substitute, zero infra change.
- **Frontend**: new `/commander` page (Risk Commander chat with COMMAND/STRATEGY modes + active triggers list). Signals page shows 3-card verification stack (Exec Mode / Noise · Entropy / Meta-Labeler) + dedicated entropy/meta-labeler veto banners. Sidebar `nav-commander` link.
- **Tests**: 108/108 backend pytest cases pass (19 new tests across 8 classes — TestIter6SignalPayload, TestRegimeAdapter, TestMetaLabeler, TestPaperTrading, TestNLStrategy, TestNLCommander, TestTimeSeriesCollections, TestPriorEndpointsRegression).
- **Bug**: Fixed leftover merge garbage in `Accounts.jsx` (broken parse from prior fork) that was blocking entire frontend compile.

## Known Limitations
- The MT5 EA itself requires a Windows MT5 terminal; this is a platform constraint, not ours.
- Live free FX feed (`open.er-api.com`) refreshes daily, not intraday. For intraday FX the user can upgrade to a paid FX API.
- XAUUSD history uses PAX Gold (PAXG) as a 1:1 proxy via CoinGecko, since AV no longer ships XAU on free tier. Spread vs. spot is small (<1%).
