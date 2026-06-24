# AI Trading Bot — STOIC · PRD

## Original Problem Statement
Build STOIC — an AI trading bot for Gold (XAUUSD) and Bitcoin (BTCUSD) with
four risk profiles (low/middle/high/extreme), live MT5 microcent execution
via a downloadable EA (Bridge pattern), JWT auth, Android-friendly responsive
UI, dual-AI intelligence (Claude Sonnet 4.5), Kelly Criterion sizing,
Regime-Adaptive Risk Modifier, Macro-freeze, and a Meta-Labeler classifier.

## Sessions changelog
- 2026-02-24 (iter-24g) — **Wrong-terminal detection + REFRESH 404 fix**:
  - **Bug 1**: Accounts page REFRESH button returned "Refresh failed, not found". Cause: frontend called `/accounts/{id}/test_connection` (underscore) but the route is `/accounts/{id}/test-connection` (hyphen). One-character fix.
  - **Bug 2**: Two STOIC accounts showed the same balance because both EAs were attached to the same MT5 terminal, so `AccountInfoDouble(ACCOUNT_BALANCE)` returned the same number for both bridge_tokens. Fix:
    - **EA v1.24**: Heartbeat now sends `account_login` (`AccountInfoInteger(ACCOUNT_LOGIN)`) and `base_currency` (`AccountInfoString(ACCOUNT_CURRENCY)`).
    - **Backend**: Cross-checks `account_login` against the STOIC account's configured `account_number`. Persists `broker_account_mismatch` + a clear human-readable reason. Test-connection now elevates severity to `warn` when mismatched.
    - **UI**: Yellow warning banner on the affected account row: "WRONG MT5 TERMINAL · EA is logged into MT5 account X, but this STOIC account is configured for Y. Run this EA on a DIFFERENT MT5 instance (or remove the duplicate). Two EAs attached to the same terminal will mirror the same balance."
  - **Tests**: 4 new tests in `test_iter24g_wrong_terminal.py` covering route-exists, mismatch detected, mismatch cleared on correct login, legacy EA (no login field) doesn't crash. 34/34 pass total.
- 2026-02-24 (iter-24f) — **Trade Audit Trail + removed Backfill button**:
  - **New**: `GET /api/trades/{id}/audit` returns the full chronological lineage for a single trade — STOIC lifecycle events (created, breakeven, partial-close, trail-active, closed) merged with every `broker_deals` row that touched the trade's mt5_ticket. Sorted by timestamp.
  - **UI**: Yellow `AUDIT` button (history icon) on every trade row with a mt5_ticket. Click → modal shows colour-coded timeline with broker timestamps, MANUAL tag for magic=0 deals, and a details grid per event (exit, P&L, deal_id, lots, etc.). Sticky header + scrollable body for trades with long lineages.
  - **Removed**: The `BACKFILL P&L` button + modal and the `PATCH /api/trades/{id}/backfill` endpoint. No longer needed — `/bridge/external-deal` (iter-24e) handles broker→STOIC sync automatically once the EA v1.23 is installed. Cross-tenant isolation verified (other-user's trade returns 404).
  - **Tests**: 5 new tests in `test_iter24f_audit_trail.py` (404 on unknown, STOIC lifecycle events present, broker deals included with MANUAL flag, cross-user 404, deprecated backfill endpoint gone). 30/30 pass across iter-24 a/b/c/e/f. (iter-24d deleted.)
- 2026-02-24 (iter-24e) — **External-deal bridge · broker-side trades auto-sync**:
  - **What changed**: The MT5 EA now reports EVERY broker deal — bot-initiated AND manual — back to STOIC via the new `/api/bridge/external-deal` endpoint. Closes the orphan-trade gap permanently: manual closes on MT5, partial-closes from the broker UI, manual position opens all round-trip back to STOIC's Trades page automatically.
  - **EA v1.23**: Added `OnTradeTransaction()` handler that fires on every `TRADE_TRANSACTION_DEAL_ADD`. Extracts `position_id`, `deal_id`, `entry direction`, symbol, price, profit, commission, swap, magic → POSTs to `/bridge/external-deal`. Users must re-download the EA from the Accounts page for this to activate.
  - **Backend**: New `BridgeExternalDeal` model + endpoint. Behaviour:
    - `deal_entry=in` + ticket NOT tracked → create new trade with `origin="external"` (manual MT5 entry)
    - `deal_entry=in` + ticket already tracked → no-op (bot already created it)
    - `deal_entry=out` + tracked trade has no exit_price → backfill `exit_price`+`pnl` (panic-orphans get healed automatically next deal)
    - `deal_entry=out` + tracked trade already has exit_price → mark `external_confirmation=True` (audit-only)
    - `deal_entry=out` + ticket NOT tracked → create fully-closed audit row so P&L still hits the user's stats
  - **Idempotency**: New `broker_deals` collection with unique index on `(deal_id, account_id)`. Replays return `{duplicate: true}` so a flaky-network EA can safely retry forever without double-applying.
  - **Realised P&L** = `profit + commission + swap` (correctly reflects broker costs).
  - **WebSocket**: Every external-deal broadcasts `trade_updated` so the Trades page refreshes live.
  - **Tests**: 6 new tests in `test_iter24e_external_deal.py` (auth 401, external open, backfill via close, idempotency replay, audit row when ticket unknown, no-op when already tracked). 30/30 pass across iter-24 a/b/c/d/e.
- 2026-02-24 (iter-24d) — **Manual P&L backfill for orphaned closed trades**:
  - **Bug**: User clicked PANIC at 05:01:37 UTC. Reconciler aggressively marked 3 demo trades `status=closed close_reason=panic` with `exit_price=null pnl=0`. User then closed those positions manually on MT5 at 05:02:40 UTC with real profits ($337.34, $430.86, $278.89). Since the EA only POSTs `/bridge/report` from its own `ClosePosition()` path (no `OnTradeTransaction` handler), the actual exit price + profit never landed in STOIC.
  - **Fix · phase 1 (ship now)**: New `PATCH /api/trades/{id}/backfill` endpoint accepting `{exit_price, pnl}`. Only allowed on closed trades whose exit_price is null — refuses silent overrides. Tags `close_reason` with `+backfill` for audit. Frontend Trades page now shows a yellow `BACKFILL P&L` button on every such orphaned closed trade. Modal walks the user through pasting the values from MT5 → History → Deal row, with auto-suggest computing P&L from exit price × lot × contract size (overridable).
  - **Fix · phase 2 (follow-up)**: Add `OnTradeTransaction()` handler to the MT5 EA so manual closes round-trip back to STOIC automatically. (Not shipping yet — user has working backfill button.)
  - **Tests**: 5 new tests in `test_iter24d_backfill.py` covering success path, refuse-when-set, refuse-on-open, negative P&L (losses), 404 on unknown id. All pass.
- 2026-02-24 (iter-24c) — **Trades page · per-account filter**:
  - **Backend**: `/api/trades`, `/api/trades/stats`, `/api/trades/live` now accept optional `?account_id=X`. Omitted = all-accounts behavior (unchanged). Each endpoint scopes the Mongo query and aggregation when present.
  - **Frontend**: New filter row on the Trades page — `ACCOUNT · [ALL ACCOUNTS] [DEMO · LIVE] [MICRO · LIVE]` (auto-hides for single-account users). Stats card refreshes when toggled. New **ACCOUNT** column inline in the trades table shows the label for every row even when "ALL ACCOUNTS" is selected.
  - **Tests**: 5 new tests in `test_iter24c_trades_filter.py` covering list/stats/live filtering, partition sum invariant, and per-account ≤ global sanity. 19/19 pass across iter-24 a/b/c.
- 2026-02-24 (iter-24b) — **Per-Account Performance Comparison widget**:
  - **Backend**: New `account_analytics.py` aggregates closed/open trades per `account_id`, joins each with per-account or default bot_config (active preset, risk level, max lot size, override flag). Returns 30-day P&L, lifetime P&L, win rate, profit factor, avg/trade, open + pending counts. Sorted by lifetime P&L desc. Endpoint: `GET /api/analytics/by-account`.
  - **Frontend**: New `PerAccountComparison.jsx` widget on Dashboard (above the chart). Side-by-side cards per account: large 30-day P&L tile + TrendingUp/Down icon, metric grid (lifetime P&L, win rate, trades, open/pending, profit factor, avg/trade), bot config footer (preset · risk · max-lot cap). **LEADER · 30D** badge auto-highlights whichever account has the highest positive 30-day P&L. Auto-hides when user has only 0–1 accounts.
  - **Tests**: 5 new tests in `test_iter24b_by_account.py` covering auth gate, response shape, all required fields, sort order, 401 on unauthenticated. 14/14 pass combined with iter-24.
- 2026-02-24 (iter-24) — **Per-account bot configs + max-lot-size cap**:
  - **What changed**: `bot_configs` collection is now keyed by `(user_id, account_id)`. Existing user-level docs become the "default profile" (`account_id=null`); each MT5 account can now have its own independent override (different risk level, symbols, presets, drawdown limits, max lot). The bot runner loops per-config — a per-account cfg pins its single account; the default cfg covers every account that does *not* have an override. Cooldowns, in-flight counts and trade-of-day caps are all scoped per (user, account) so the bots run truly independently.
  - **New field `max_lot_size`** (float, 0=uncapped) on bot_config. `bot_runner` clamps the AI-computed lot to this ceiling before handing to the engine.
  - **API**: `GET /api/bot/config?account_id=X`, `GET /api/bot/configs` (list), `PUT /api/bot/config?account_id=X`, `DELETE /api/bot/config?account_id=X` (revert to default), `POST /api/bot/start|stop?account_id=X`. All ?account_id params optional → default scope when omitted. Account ownership is validated server-side (404 on unknown).
  - **UI**: New AccountScopeBar at the top of `/bot` showing Default + every account tile (ON/OFF/USES-DEFAULT badges). Click a tile → page edits that scope. "RESET TO DEFAULT" button removes the per-account override. New "MAX LOT SIZE PER TRADE" field in Execution Behaviour with footer hint indicating per-account vs default.
  - **Migration**: `seed.py` backfills `account_id=null` on legacy docs, drops the old `user_id` unique index, creates composite `(user_id, account_id)` unique index. Zero data loss.
  - **Cleanup**: Deleting an account now also removes its per-account bot_config override.
  - **Tests**: New `test_iter24_per_account_bot_cfg.py` — 9/9 pass covering default profile, auto-create on read, max_lot persistence + negative clamp, scope isolation, start/stop independence, 404 on unknown account, delete-revert flow.
- 2026-02-23 (iter-23.2) — **Reconciler hardening + REFRESH UX**:
  - **Bug**: User reported "I click REFRESH but nothing happens." Investigation showed two stacked problems: (a) the REFRESH button had no spinner/disabled feedback so successful no-op refreshes felt broken; (b) the 3 visible "PENDING" trades were attached to a **deleted account_id** (`6a392be8…`) that no longer existed in `db.accounts`. The previous reconciler iterated only by current accounts, so it never inspected these orphans. They were also stuck in `status="pending"+close_requested=True` (user had clicked × CLOSE), which the original reconciler only-matching `status="open"` skipped.
  - **Fix**: (1) `reconcile_account` now matches both `status=open` AND `status=pending+close_requested=True`; (2) `reconcile_user` adds a **Phase 1 sweep** for trades whose `account_id` no longer exists in the user's accounts (closes them with `close_reason="account_deleted"`); (3) Trades page REFRESH button gets `refreshing` state, spinning icon, "REFRESHING…" text, disabled while in flight.
  - **Tests**: 4 new regression tests in `test_iter23_reconciler.py` (pending-close orphans, pending-open-no-touch, account-deleted sweep, end-to-end count-fallback with pending). 9/9 pass.
  - **Live verify**: Admin's 3 stuck trades closed on first SYNC WITH BROKER click, with `close_reason="account_deleted"`.
- 2026-02-23 (iter-23) — **Trade reconciliation (broker→DB sync) · CRITICAL FIX**: User reported 3 trades showing OPEN in STOIC after the broker had stop-out-closed them. Root cause: the bot was 100% reliant on the EA explicitly POSTing `/bridge/report` with status=closed; if the EA missed that call (crash, network blip, broker stop-out timing) trades stayed open forever. Fix: (a) extended `BridgeHeartbeat` model with optional `open_tickets: list[int]` (EA v1.22+); (b) new `/app/backend/trade_reconciler.py` with `reconcile_account()` + `reconcile_user()` — closes any DB-open trade whose `mt5_ticket` is NOT in the EA's reported open list, marks `close_reason="broker_reconciled_<source>"` and `reconciled=true`; (c) wired auto-reconciliation into the heartbeat handler (every ~5s tick); (d) new `POST /api/trades/reconcile` user-triggered endpoint with count-based fallback for older EAs (when `open_positions==0` we know unambiguously all DB-opens are stale); (e) amber "SYNC WITH BROKER" button on the Trades page header. Pytest suite: 5/5 in `test_iter23_reconciler.py` cover all-closed, partial-still-open, old-EA-skip, count-fallback, and pending-trade-no-ticket protection. Frontend smoke verified the button renders and is wired to the endpoint.
- 2026-02-23 (iter-22) — **Public Affiliate Landing Page**: New unauthenticated route `/affiliates` at `/app/frontend/src/pages/AffiliateLanding.jsx`. Single-page marketing flow designed to convert cold visitors → applications: hero with "Refer one trader. Get paid every month they trade." + 4-stat strip (20% / 60D / $50 / Tier 1), 3-step How-It-Works (Apply/Share/Earn), interactive Earnings Calculator with live sliders (1-200 referrals × $19-199 plan → monthly/year/3y projections), Why-STOIC value props (high-LTV audience, pre-built media kit, transparent attribution), 5-question FAQ accordion, collapsible full Agreement, final CTA, footer disclaimer. Custom gold-grid hero background + radial gold-glow corner. New `data-testid`s on every interactive element. Discoverability links added to Login + Register pages ("EARN 20% RECURRING · BECOME AN AFFILIATE →"). Lint clean. Smoke verified: slider updates monthly value, FAQ expands, terms expands, CTA routes (/register, /login) correct.
- 2026-02-23 (iter-21 testing) — **Full regression pass · 21/21 tests · zero bugs**. New regression suite at `/app/backend/tests/test_iter21_presets_and_payouts.py` covers built-in presets (list, apply, 404), custom preset CRUD (create, list, apply, delete, dupe-name 400, empty-name 400, 11th-preset limit 400, user isolation 404), affiliate payout (no-affiliate 404, insufficient-balance 400, admin auth 403, invalid id 400, end-to-end process flow that flips status + commissions paid + zeroes balance), and Co-Pilot smoke (real Claude call with session continuity). Co-Pilot Claude assistant already shipped in earlier iterations (CoPilotWidget.jsx + copilot.py + copilot_routes.py) — grounded in live bot_config/signals/trades/accounts/regime/panic/triggers, with multi-turn session memory and bug-screenshot intent detection. Mounted on every authenticated page.
- 2026-02-23 (iter-21) — **Custom Presets + Affiliate Payout self-service**:
  - **Save My Own Preset** — new `backend/user_presets.py` (whitelist of 18 behaviour fields, 10-preset/user cap, dupe-name guard). New endpoints: `POST /api/bot/my-presets`, `DELETE /api/bot/my-presets/{id}`. `/api/bot/presets` now returns `{presets, custom}`. `/api/bot/preset/{key}` accepts `custom:{preset_id}` keys. Frontend BotConfig: "Save Current as Preset" button → modal (name + desc, 40/200 char limits), "Your Presets · N/10" purple-themed sub-grid with hover-to-reveal delete trash icons. Verified via curl: create → list → apply-custom → dupe-name 400 → delete. Frontend screenshot confirms modal + custom card rendering.
  - **Affiliate Payout Request** (closing the gap on existing affiliate system) — new endpoints `POST /api/affiliate/request-payout` (eligibility: unpaid balance ≥ $50, one open request at a time), `GET /api/affiliate/payout-requests`, admin `GET /admin/affiliate/payout-requests` + `POST /admin/affiliate/payout-requests/{id}/process` (flips all pending commissions to paid and zeros affiliate balance). New `affiliate_payout_requests` collection. Frontend Affiliate page: PAYOUT STATUS row with eligibility message + REQUEST PAYOUT button (disabled below $50). Verified: HTTP 400 on insufficient balance.
- 2026-02-23 (iter-20) — **Competitive UX uplift (SmartBotScalper-inspired)**:
  - **Strategy Presets** — 7 named one-click personas (Sniper, Scalper, Trend Rider, Breakout Hunter, Mean Reversion, Aggressive, Balanced). New module `backend/strategy_presets.py` + endpoints `GET /api/bot/presets`, `POST /api/bot/preset/{key}`. New `active_preset` field on `bot_configs`. Frontend BotConfig page gains a top section with 7 cards, click-to-apply with toast confirmation + ACTIVE badge.
  - **Risk-tier multiplier badges** — Low ×0.5, Medium ×1.0, High ×2.5, Extreme ×5.0 rendered as inline pills on risk cards.
  - **Live Ticker Tape** — new `TickerTape.jsx` infinite-scroll marquee pinned above every authenticated page. Sources: `/market/quotes` (XAUUSD, BTCUSD) + `/agents/macro` (DFF, DGS10, VIX, T10YIE, UNRATE). Hover-to-pause via CSS. Refreshes every 30s.
  - **Trust/Status Bar** — slim `StatusBar.jsx` above ticker: SERVICE · OPERATIONAL · ENCRYPTED LOGIN · EU SERVERS · 24/7 · "YOUR FUNDS STAY WITH YOUR BROKER". Mounted in `AppLayout.jsx`.
  - Lint clean (Py + JS). Backend smoke: 7 presets listed, sniper apply → min_conf_override=75 + active_preset="sniper", bad key → HTTP 404. Frontend smoke: status-bar + ticker-tape + 7 preset cards + 4 multiplier badges + Sniper ACTIVE pill verified.
- 2026-02-23 (iter-17) — **Multi-Agent Architecture**: Introduced formal `agents/` package implementing the Research → Strategy → Risk → Execution pipeline. New modules: `agents/research_agent.py` (news, macro, FRED), `agents/strategy_agent.py` (wraps `ai_signals.analyze_symbol`), `agents/risk_agent.py` (portfolio-level checks — adds cross-asset correlation veto on XAU↔BTC when r ≥ 0.7), `agents/execution_agent.py` (broker routing), `agents/orchestrator.py` (coordinator + persists `agent_activity` log per tick). Bot-runner now routes every tick through `orchestrator.analyze_tick()`. New `macro/fred.py` pulls 5 FRED series (DFF, DGS10, T10YIE, UNRATE, VIXCLS) keyless via `fredgraph.csv`, cached 6h. New API: `GET /api/agents/activity`, `GET /api/agents/latest`, `GET /api/agents/macro`. New `/agents` UI page with agent roster, live macro snapshot card, and tick-by-tick activity feed (auto-refreshes 30s). 13 new unit tests in `test_iter17_agents.py` — **70/70 backend tests pass** (57 existing + 13 new). Frontend lint clean. End-to-end smoke verified — orchestrator already ran 3 live XAUUSD ticks visible in activity feed.
- 2026-02-23 (iter-16) — **P0 Profitability Pack**: (a) Verified ATR-adaptive SL/TP was already live (`ATR_SL_MULTIPLIER=1.5`, `ATR_TP_MULTIPLIER=5.0`); (b) Added **Post-SL Cooldown** — blocks new entries on a symbol for 45 min after a stop-out (configurable). New helper `_on_sl_cooldown()` in `bot_runner.py`, intelligence counter `sl_cooldown_block`; (c) Added **Pre-News Position Protector** — new module `position_protector.py` flattens OPEN trades 5 min before HIGH-impact macro events (NFP/CPI/FOMC) via `pending_modification={type: FULL_CLOSE}`; broadcasts WebSocket `position_protected` event + Telegram alert (`notify_pre_news_close`); intelligence counter `pre_news_protect`; (d) Added **Liquidity-Window Booster** for XAUUSD — lowers min_confidence by 3 pts during London-NY overlap (clamp 70), raises by 4 in off-hours (clamp 92); surfaces `liquidity_window` block on every signal payload. New config fields surfaced through `models.py` + `bot_routes.py` + BotConfig UI toggles. Guide §5 + 4 new FAQ entries updated. 12 new unit tests in `test_iter16_profit_pack.py` — 57/57 total backend tests passing.
- 2026-02-23 — **Guide page autopilot rework**: Added new Section 7 "Autopilot mode explained" with a 7-stage lifecycle diagram (Tick → Dual-AI → Cascade → Sizing → EA Order → Server-side monitor → Guards) and an 8-item pre-flight checklist. Renumbered TOC, sharpened Step 6 with exact toggles (START BOT, Auto-Execute = ENABLED, Default MT5 account, Symbols list) and a VPS callout in Step 3 for 24/7 operation. Intro now leads with "complete autopilot" + a TL;DR Callout. User-verified via screenshots.
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

- 2026-06-23 (iter-15) — **Self-service operations + content** (this fork):
  - **Billing page** (`/billing`) — current plan, expiration with day-countdown,
    auto-renew status, full transaction history. Smart CTA adapts to plan
    state (RENEW / MANAGE / ADMIN GRANT).
  - **FAQ page** (`/faq`) — 40 hand-written entries in 8 categories
    (Getting Started, AI Signals, Risk, MT5 Bridge, Billing, Security,
    Notifications, Troubleshooting) with search + category filter + accordion.
  - **Guide page** (`/guide`) — sticky-TOC docs site with 10 sections covering
    what STOIC is, why it's different vs retail bots, the dual-AI engine,
    full 10-veto cascade, risk guards, step-by-step setup (6 steps),
    daily workflow (5 min/day), advanced tuning + audit, going-live path
    (paper → real), and quick-link launcher.
  - **Bulk-clear** for Signals (`DELETE /api/signals`, scope=all|hold|non_hold
    + older_than_days) and Trades (`DELETE /api/trades`, scope=all|closed|
    cancelled|failed, with hard refuse on open/pending).
  - **HoldReasonBanner** + **StrengthBanner** — every signal card now has a
    plain-English audit line; BUY/SELLs get a 0-100 strength score with
    component-level concerns/strengths chips.
  - **Signal filter chips** — STRONG / SOLID / MARGINAL / BLOCKED / ALL HOLDS,
    live counts, auto-disabled empty buckets.
  - **Telegram safety net** — `_is_plausible_trade()` in `notifier.py` blocks
    obviously-synthetic alerts (mock tickets < 1M, entry > 50% off live quote).
    Combined with the pytest conftest + `notified_opened` flag = 3-layer
    defence against phantom Telegram messages.
  - **Real bug fixes**: `anti_tilt_active` was set but never enforced — now
    correctly freezes new entries when tripped. Stripe webhook stack-trace
    spam fixed (CheckoutError catch). PUT `/api/bot/config` switched to
    PATCH semantics (partial updates no longer wipe other fields).
  - **Sidebar** — added Billing, Guide, FAQ entries. Sidebar is now 15 items.
  - **Tests**: regression suite still green; pytest `conftest.py` autouse
    fixture silences ALL outbound notifications during tests.
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
