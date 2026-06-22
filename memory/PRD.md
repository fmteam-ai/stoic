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

## CHANGELOG · Feb 22, 2026 — Stripe Subscriptions + Bug Reports
- **New: Screenshot Bug Reports via Co-Pilot** — `BUG_INTENT` regex in `CoPilotWidget.jsx` detects bug-report intent (e.g. "file me a screenshot bug report"). Captures viewport via `html-to-image` (`skipFonts: true`) + global console log buffer (50-event ring installed in `index.js`) + URL + UA. Shows a draft form (`bug-draft`) with screenshot preview, description textarea, cancel/submit. POST to `/api/bugs`. Admin-only `GET /api/bugs`, `GET /api/bugs/{id}`, `PATCH /api/bugs/{id}/status`.
- **New: Stripe Subscriptions (prepaid model)** — 4 plans:
  - Monthly: $49 / 1 month (no discount)
  - Quarterly: $132.30 / 3 months (10% off, ~$44.10/mo)
  - Semi-annual: $235.20 / 6 months (20% off, ~$39.20/mo)
  - Annual: $352.80 / 12 months (40% off, ~$29.40/mo)
- Implemented as prepaid plans rather than true Stripe recurring (emergentintegrations StripeCheckout wrapper is one-time payment). Each successful payment extends `valid_until` by `duration_months * 30 days`. Idempotent via `applied` flag on `payment_transactions`. No surprise auto-renew — users explicitly renew.
- **Admin grandfathered**: admin role gets `valid_until` set ~10 years out at first request; `/api/subscription/checkout` returns 400 for admin to prevent foot-gun.
- **30-day grace period** for pre-existing non-admin users (`LEGACY_GRACE_DAYS=30` in `subscription_service.py`).
- **Bot runner entitlement gate**: when `subscription_active()` returns inactive, `_process_user` filters accounts to paper-mode only; if no paper accounts, returns early.
- **Endpoints**: `GET /api/subscription/plans|status`, `POST /api/subscription/checkout`, `GET /api/subscription/poll/{sid}`, `POST /api/webhook/stripe`.
- **Frontend**: `/subscription` page (4-card pricing grid with savings badges, admin grandfather banner, prices formatted as 2dp), `/subscription/success` (polls /poll every 2s up to 10×), top-bar `SubscriptionBanner` nag (hidden for admin / grace > 7d / active > 7d), `nav-subscription` sidebar link.
- **Hardened**: Stripe errors now return sanitised 502s ("Could not start Stripe checkout. Please try again shortly.") with full server-side exception logs.
- **Tests**: 144/144 backend pytest (26 new across TestBugReports, TestSubscription* — including TestAdminCannotSubscribe regression). 100% frontend on /subscription render + Co-Pilot bug-report end-to-end.

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

## CHANGELOG · Jun 22, 2026 — Profit Protection Suite + Manual Trade + Encrypted Broker Creds + Bot Status
- **New: Profit Protection Suite** — server-side `trade_manager.py` async loop (15s default) that supervises every open MT5 trade. Three features:
  1. **Break-Even Auto-Shift**: after +1R, SL moves to entry (zero-risk runner).
  2. **Partial Close at TP1**: closes 50% of the lot at +1R, runner stays.
  3. **Trailing Stop-Loss**: after +1.5R, SL trails behind price at 0.7R distance, 0.2R step gate.
- **New: Daily Drawdown Circuit Breaker** — auto-stops the bot when realised P&L for today crosses -3% (configurable). Broadcasts `circuit_breaker_tripped` WS event.
- **Bridge protocol v1.10**: `poll-trades` now returns `{trades, modifications}`. EA v1.10 handles `MODIFY_SL` and `PARTIAL_CLOSE` actions and posts back to `/api/bridge/modification-ack`.
- **BotConfig** gained 11 new fields: breakeven_enabled/_trigger_r, partial_close_enabled/_trigger_r/_fraction, trailing_enabled/_start_r/_distance_r, daily_drawdown_enabled/_pct. Default values match conservative best-practice (1R BE, 1R 50%-close, 1.5R trail start, -3% DD).
- **Trade model** gained `original_stop_loss`, `original_lot_size`, `breakeven_set`, `partial_closed`, `trail_active`, `pending_modification` fields. Trades page shows BE / PC / TRAIL / SYNC badges per row.
- **New: `/api/bot/status` endpoint** — rich runtime status (last tick, last action+confidence, next tick ETA, why_no_trade explanation, open_trades count). Polled every 10s by Dashboard.
- **New: Dashboard BOT STATUS strip** — pinned below Portfolio Performance, shows live state (TRADING / HOLDING / VETOED / STOPPED), last tick ago, last action, next tick countdown, auto-execute status, plain-English "why no trade" reason + AI reasoning excerpt.
- **New: Manual Test Trade modal** (`/api/trades/manual`) — paper-only manual order placement at live mid-price, bypasses AI confidence gating. Gold button on Signals page.
- **New: Encrypted Broker Credentials Vault** — optional AES-256-GCM storage of MT5 investor + master passwords on live accounts. Endpoints: `POST/PATCH /api/accounts/{id}/credentials*`, `POST /api/accounts/{id}/credentials/reveal`. Frontend shows ADD/UPDATE/REVEAL/CLEAR per account; reveal auto-hides 30s.
- **Dashboard polish**: Portfolio Performance (open trades / total / win rate / total P&L) moved to top of page above quote tiles. WS toast for trade_management events (BE/TRAIL/PC).
- **Fix: webpack-dev-server v5/CRA 5.0.1 incompat** — pinned `webpack-dev-server: 4.15.2` in `resolutions` (frontend was crashing on start because v5 dropped `onAfterSetupMiddleware` and `https` config options CRA still emits).

## CHANGELOG · Jun 22, 2026 — Telegram Push Notifications
- **New: Telegram Bot Integration** — per-user push alerts to a personal Telegram chat. Direct httpx calls to `api.telegram.org/bot{token}/sendMessage` (no SDK, no webhooks). Bot tokens stored AES-256-GCM encrypted via existing `secrets_vault.py`.
- **Events**: `trade_opened`, `trade_closed`, `breakeven`, `partial_close`, `trail` (opt-in default off due to frequency), `circuit_breaker`, `high_conf_signal` (≥75%). Per-event opt-in/out.
- **Wiring**: `notifier.py` module with `notify_*` helpers; hooked into `bridge_routes.report` (live open/close), `execution.PaperEngine.execute` + `settle_paper_trades_against_price` (paper lifecycle), `trade_manager._manage_one_trade` (BE/PC/TRAIL), `_check_daily_drawdown` (circuit breaker), `bot_runner._process_user` (high-conf signal). All call sites use try/except to never block trade execution.
- **Endpoints**: `GET/PUT /api/notifications/telegram`, `POST /api/notifications/telegram/test` (sends a Markdown-V2 formatted test message + surfaces Telegram API errors as 400s with the description field).
- **Frontend**: `/notifications` page with 3 sections (Setup guide with deep links to @BotFather + @userinfobot, encrypted token + chat ID + master enable, per-event toggle grid). Sidebar entry `nav-notifications` (Bell icon). Security note explains encryption + token revocation.
- **Bridge protocol**: `notified_opened` flag on trades prevents duplicate "Trade Opened" pings if EA re-reports.

## CHANGELOG · Jun 22, 2026 — 2-Way Telegram Control Plane
- **New: Inbound Telegram commands** (`routes/telegram_routes.py`) — Telegram webhook at `POST /api/telegram/incoming/{secret}` receives Update objects, dispatches to command handlers, replies via sendMessage. Per-user 32-byte URL-safe `webhook_secret` stored in `notifications` collection.
- **Security**: Two-layer auth — (1) webhook URL contains per-user secret, (2) handler verifies `message.chat.id` matches the user's configured chat_id (prevents impersonation if URL leaks). Bad/unknown updates always return 200 to avoid Telegram retry storms.
- **Commands**: `/start /help /status /pnl /trades /balance /run /stop /panic /close [SYMBOL]`. All Markdown-V2 escaped, formatted nicely with emojis. `/close XAUUSD` marks all open trades on that symbol for close (EA picks up close_requested=True). `/panic` mirrors `/api/panic` logic inline (stops bot, cancels pending, marks open for close).
- **Activation endpoints**: `POST /api/telegram/webhook/enable` (calls Telegram setWebhook with our URL), `POST /api/telegram/webhook/disable` (calls deleteWebhook, clears secret), `GET /api/telegram/webhook/status`.
- **Route ordering**: webhook is at `/incoming/{secret}` (not `/webhook/{secret}`) to avoid clashing with `/webhook/enable|disable|status` literal paths.
- **Frontend**: New Section 04 on `/notifications` page — command reference grid (8 commands), ACTIVATE/DISABLE button, status pill, webhook URL display when active. Disabled until a bot token is saved.

## Backlog Updates (P1/P2 still pending)
- **P1** — Spread/Slippage filter (reject signals when spread > 2× 24h median).
- **P1** — Multi-timeframe confluence (require H4 trend to match H1 signal direction).
- **P2** — Performance Attribution dashboard (engine × regime × symbol × hour-of-day P&L breakdown).
- **P2** — Session-aware bias (London / NY overlap / Asian regime detection).
- **P2** — Telegram / Email daily digest.
- **P3** — Triple-AI consensus (GPT-5.2 + Claude + Gemini votes).
- **P3** — Backtesting engine (replay 6m history against AI signal generator).

## Known Limitations
- The MT5 EA itself requires a Windows MT5 terminal; this is a platform constraint, not ours.
- Live free FX feed (`open.er-api.com`) refreshes daily, not intraday. For intraday FX the user can upgrade to a paid FX API.
- XAUUSD history uses PAX Gold (PAXG) as a 1:1 proxy via CoinGecko, since AV no longer ships XAU on free tier. Spread vs. spot is small (<1%).
