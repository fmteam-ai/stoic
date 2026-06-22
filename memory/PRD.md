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
- **P1** — Background scheduler: when bot is `active`, auto-generate signals every N minutes per symbol and (if `auto_execute=true` and tradeable) push to the EA queue.
- **P1** — Brute-force protection on login (lockout after N failed attempts).
- **P2** — WebSocket push for live prices and trade updates (instead of 60s polling).
- **P2** — Custom symbol add (let users register tickers not in the default map).
- **P2** — Backtest engine — replay 6-month history against the AI signal generator + risk profile to surface historic equity curve.
- **P2** — Daily P&L email digest (Resend integration).
- **P3** — Native Android shell via Capacitor / React Native (PWA already works).

## Known Limitations
- The MT5 EA itself requires a Windows MT5 terminal; this is a platform constraint, not ours.
- Live free FX feed (`open.er-api.com`) refreshes daily, not intraday. For intraday FX the user can upgrade to a paid FX API.
- XAUUSD history uses PAX Gold (PAXG) as a 1:1 proxy via CoinGecko, since AV no longer ships XAU on free tier. Spread vs. spot is small (<1%).
