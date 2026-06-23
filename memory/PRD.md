# AI Trading Bot — STOIC · PRD

## Original Problem Statement
Build STOIC — an AI trading bot for Gold (XAUUSD) and Bitcoin (BTCUSD) with
four risk profiles (low/middle/high/extreme), live MT5 microcent execution
via a downloadable EA (Bridge pattern), JWT auth, Android-friendly responsive
UI, dual-AI intelligence (Claude Sonnet 4.5), Kelly Criterion sizing,
Regime-Adaptive Risk Modifier, Macro-freeze, and a Meta-Labeler classifier.

## Sessions changelog
- Earlier sessions: pip-based scaling (SL 150 / TP1 100 / TP2 200 / TP3 300),
  Profit Protection Suite (BE shift, trailing), Telegram 2-way bot,
  Performance Attribution Analytics, Encrypted Broker Vault, Bot Status Strip.
- 2026-06-22 (this fork) — **Trading Intelligence pack**:
  - Auto-Tune confidence threshold from per-bucket historical win-rates
    (`auto_tune.py` + `GET /api/analytics/auto-tune`, refresh endpoint).
  - MT5 Spread Filter — EA v1.21 sends per-symbol spreads on every heartbeat;
    bot_runner blocks auto-execute when current spread exceeds per-symbol cap.
  - Multi-Timeframe Trend Gate — new veto in `ai_signals.py` enforcing trend
    confluence (SMA20 slope · SMA50 vs SMA200 · price vs SMA50).
- 2026-06-22 (iter-9) — **Affiliate gate + Slippage veto + Intelligence counters**:
  - Affiliate Program is now gated behind an active paid subscription.
    `/api/affiliate/apply` returns HTTP **402 Payment Required** when the user has
    no active sub. `/api/affiliate/status` surfaces a new `subscription_required`
    flag and a `subscription_required` state for fresh, un-subscribed users.
    Frontend renders a `SubscriptionGate` card with a CTA to `/subscription`.
  - **Server-side slippage veto** — on first `bridge /report` open, the bot
    compares `actual_entry` vs the signal's `intended_entry` and computes
    `slippage_pips`. If above `max_slippage_pips` (per-symbol cap on BotConfig),
    the trade is force-closed via `pending_modification={type:FULL_CLOSE}` and
    `close_reason=slippage_veto`.
  - **Intelligence counters** — daily rolling counts of `mtf_veto`,
    `auto_tune_block`, `spread_block`, `slippage_veto` exposed under
    `/api/bot/status.intelligence` (today + yesterday sum). Dashboard Bot
    Status Strip now shows a "Vetoes Today (24h)" pill row.
  - Frontend: Bot Config Section 05 gains a Slippage Veto tile + per-symbol
    pip caps (`slippage-cap-{SYMBOL}`).
  - Tests: 8 new (TestAffiliateSubGate ×3, TestIntelligenceCounters ×2,
    TestSlippageVeto ×3). **Full suite: 164/164 passing.**
  - Frontend: Bot Config "Section 05 · Trading Intelligence" + Analytics
    "Auto-Tuned Thresholds" card with 9-bucket histogram.
  - Tests: 12 new (TestMtfGate ×5, TestAutoTune ×4, TestSpreadFilter ×3).
  - **Regression: 156/156 backend tests passing.**

## Active features (live)
- Live MT5 execution via signed Bridge token (EA v1.21).
- Paper accounts with simulator + manual close.
- Dual-AI Claude Sonnet 4.5 + indicators engine.
- Risk profiles: low/medium/high/extreme.
- Kelly sizing + Regime Adapter + Macro freeze + Meta-Labeler veto.
- Multi-Timeframe trend gate (always-on).
- Auto-Tune min-confidence threshold (per user, per symbol).
- MT5 Spread filter (per-symbol pip cap; needs EA v1.21+).
- Server-side Slippage Veto (per-symbol max-pip cap, force-close).
- Affiliate Program gated behind active paid subscription.
- Intelligence counters exposed under `/api/bot/status.intelligence`.
- **NEW** Live current price + Live P&L + Risk Thermometer on /trades.
- **NEW** SL-imminent Telegram alerts when ETA drops below 5 minutes.
- Profit Protection (BE shift, trailing, partial close at TP1/TP2/TP3).
- Daily Drawdown Circuit Breaker.
- Telegram push + 2-way bot (`/close`, `/panic`, `/trades`).
- Performance Attribution Analytics (multi-dimensional).
- Encrypted Broker Password Vault (AES-256-GCM).
- Stripe subscriptions + Affiliate program (gated).

- 2026-06-22 (iter-12) — **Settings page + Capital-Preservation Guards** (this fork):
  - **Capital Guards** (`bot_runner.py`): Anti-Tilt freeze (pause after N
    consecutive losses for X hours), Trade-of-Day Cap (per-symbol, per-UTC-day),
    Asia-Session Skip for XAU (00:00–07:00 UTC chop graveyard).
    UI controls in BotConfig Section 06 (`capital-guards-section`).
  - **Settings page** (`/settings`, sidebar `nav-settings`): Profile update,
    Change Password (bcrypt re-hash), TOTP 2FA enroll (`pyotp` + `qrcode[pil]`)
    with QR data-URL + 8 single-shot recovery codes (bcrypt-hashed at rest,
    plaintext shown ONCE).
  - **Login 2FA gate**: `/api/auth/login` now accepts optional `totp_code`.
    When 2FA is enabled and code missing, returns 401 `"2FA code required"`;
    `Login.jsx` reveals `login-2fa-input` on that signal. Recovery codes work
    in place of TOTP and are single-use.
  - **New deps**: `pyotp==2.10.0`, `qrcode==8.2`.
  - **Tests**: +19 (`test_iter12_settings_2fa.py`). **Suite: 207/207 passing.**

- 2026-06-23 (iter-14) — **Gold Edge Pack** (this fork):
  - **Macro Sentinel — DXY Gate** (Veto #10): pulls daily DXY from Yahoo Finance
    (`DX-Y.NYB`, stooq fallback), computes EMA-20 + 5-day slope, classifies
    regime as `bullish_usd` / `bearish_usd` / `neutral`. XAU BUYs are vetoed
    when DXY is bullish-usd, XAU SELLs vetoed when bearish-usd. Non-XAU
    symbols pass through. Snapshot cached 1h in `dxy_cache`.
  - **Weekly Drawdown Kill-Switch**: rolling 7-day P&L kill-switch in
    `circuit_breakers.check_and_trip` alongside daily. Per-user thresholds via
    BotConfig (`weekly_drawdown_pct` default 7%, `weekly_drawdown_enabled`
    default true). Daily takes precedence over weekly when both would trip.
  - **Session-Specific LR Models**: `learned_meta.retrain()` now trains a
    global artifact + up to 3 per-session artifacts (ASIA 00–07, LONDON 07–13,
    NY 13–22 UTC). Needs ≥25 samples per session AND both classes. Inference
    auto-picks the artifact for the CURRENT UTC session, falls back to global.
  - **Bot Config PATCH semantics**: `PUT /api/bot/config` now uses
    `exclude_unset=True` — partial updates no longer reset unsent fields.
  - **DXY pill** added to the Signals VetoCascade strip; **Weekly Drawdown**
    toggle added to BotConfig Section 04.
  - **News blackout** (Veto #3) was already shipped in earlier iter — covered
    by `macro_freeze_check`. No new work.
  - **Claude news sentiment** (Veto #1) was already live via `news.py` +
    NewsAPI key. No new work.
  - **Tests**: +16 new in `test_iter14_gold_edge.py`. **Suite: 233/233 passing**
    (legacy `TestApplyPaymentIdempotency` flaky in full-suite run but green in
    isolation — pre-existing, unrelated).

## Roadmap (priority order)
- **P1** Binance live BTC execution via CCXT.
- **P1** Macro Climate widget on Dashboard (live DXY regime + news-blackout countdown).
- **P2** Surface `mtf_veto` / `auto_tune_block` block reasons inline on Signals page.
- **P2** Per-user slippage analytics widget (avg slippage by symbol/time).
- **P2** Trusted-device "remember this browser for 30 days" for 2FA.
- **P2** Retail-positioning fader (Myfxbook/FXSSI % long XAU contrarian veto).
- **P2** Partial-close ladder + chandelier-exit trail.
- **P2** Walk-forward auto-retune of confluence weights every 4 weeks.

## Test credentials
See `/app/memory/test_credentials.md`.

## Key endpoints (new this session)
- `GET /api/analytics/auto-tune` — per-symbol thresholds + 9-bucket histogram.
- `POST /api/analytics/auto-tune/refresh` — invalidate cache.
- `POST /api/bridge/heartbeat` — now accepts optional `spreads` dict.
- `PUT /api/bot/config` — new fields: `spread_filter_enabled`,
  `max_spread_pips` (per-symbol pips), `auto_tune_enabled`.

## Notes / Gotchas
- The recurring "code review report" pasted into this chat is a false-positive.
  Do **not** refactor `ai_signals.py` / `bot_runner.py` / `CoPilotWidget.jsx` /
  `Dashboard.jsx` based on it. Tests stay green at 156/156.
- EA v1.21 ships in `/app/backend/static/EmergentTradingBridge.mq5` — users on
  v1.10/v1.20 keep working (spreads field is optional + backward-compat).
- MTF gate is permissive on chop (needs 2/3 disagreements to veto), strict
  on counter-trend setups.
