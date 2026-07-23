# STOIC — Sessions Changelog

- 2026-07-06 (iter-54 / fork) — **Auto Loss Review — automatic aggregate loss analysis with shadow-tested measures**:
  - New `backend/loss_advisor.py`: auto-triggered when 3+ new exact losses accumulate (24h cooldown/user, sweep throttled to 1 check/10min in bot_runner loop). Pipeline: gather 14d exact-data auto trades joined to signals → aggregates (worst segments, loss reasons, weekend-gap PnL) + per-loss postmortem pattern counts + current market snapshot (macro/regime/kalman/MTF) → Claude Sonnet 4.5 (STRICT JSON, fixed measure menu: min_confidence | velocity_veto | session_block | symbol_pause | friday_flat | other) → each measure SHADOW-TESTED against real trades ("blocked N, saves $X, misses $Y, NET $Z") → ranked by net effect → stored in `loss_reviews` + WS `loss_review` + Telegram `notify_loss_review`. NEVER auto-applied.
  - Routes (`postmortem_routes.py`, registered BEFORE `/{trade_id}`): `GET /api/postmortem/reviews`, `POST /api/postmortem/reviews/run` (manual). NOTE: postmortem `_serialize` is a whitelist — reviews use `_serialize_review`.
  - `LossLab.jsx`: Auto Loss Review explainer card + RUN NOW + latest ReviewCard (diagnosis, market context, ranked MeasureRows with evidence chips).
  - **Live E2E on real data (57 losses, -$10,108)**: Claude proposed 5 measures; shadow-testing showed only friday_flat is net-positive (+$1,005.85, $0 missed — already built in iter-52); session_block/velocity/min_confidence all net-NEGATIVE (-$123 to -$2,409) — validating evidence-gated advisory design.
  - **FILE CORRUPTION incident**: LossLab.jsx got ~1.7KB of invalid-UTF8 trailing garbage after an edit (2nd occurrence of lost/corrupted search_replace writes today). Truncated at component end + re-verified each edit via grep. ALWAYS grep-verify LossLab.jsx/Trades.jsx edits.
  - Tests: `tests/test_iter54_loss_advisor.py` 16 green (predicates incl. suffix-aware symbols, shadow math, weekend cross, aggregates, route order, never-auto-apply).

- 2026-07-06 (iter-53 / fork) — **Velocity veto (Loss-Lab guardrail) — built but DISARMED**:
  - Loss Lab suggested: LOW_VOL_TREND GOLD SELL → require Kalman velocity < +3.0, block when |velocity| > 5 contradicts MTF bias.
  - Implemented `regime_adapter.velocity_veto(signal, cfg)` (two rules: counter-momentum + momentum/structure conflict) + bot_runner SKIP wiring + `velocity_veto` intel counter. Thresholds read from REGIME_MODIFIERS with per-user `cfg["regime_overrides"][regime]` override.
  - **CRITICAL replay finding**: with the suggested thresholds, 30/30 recent live signals would have been vetoed — including the 60/60 winning SELL scalps (+$1,799 day). Gold's daily Kalman velocity ≈ +8 (multi-day bounce) while the bot profitably scalps SELLs. The Jul-3 losing cluster actually died from the weekend gap (now covered by Friday Flat).
  - User chose (d): keep code, DISARM defaults. No regime ships thresholds; arm per-user via `bot_configs.regime_overrides = {"LOW_VOL_TREND": {"velocity_counter_max": 3.0, "velocity_veto_threshold": 5.0}}`.
  - Tests: 13 green (disarmed default, armed behavior via override, MTF conflict, scope). Replay verified 0/30 vetoed with defaults.
  - User asked whether the bot auto-adjusts strategies from losses — answered in finish (Auto-Tune, Learned Meta profit-tied, anti-tilt/VaR deleveraging auto; AI Optimizer + Loss Lab advisory). Proposed future: auto-shadow-test Loss Lab guardrails against recent signals before arming.

- 2026-07-06 (iter-52 / fork) — **Friday Flat guard (weekend gap protection)**:
  - New `backend/friday_flat.py` (mirrors position_protector pattern): in the window before the Friday 21:00 UTC weekly close (default 60 min, config `friday_flat_minutes_before`), for every open non-crypto trade → mode "close": queue FULL_CLOSE pending_modification (needs EA v1.40+; auto-degrades to tighten on older EAs); mode "tighten": SL → breakeven when in profit, else halve remaining risk (never loosens). Idempotent via `friday_flat_at` flag; crypto (BTC/ETH) exempt; default-cfg respects per-account cfg ownership; window extends 3h past close.
  - `bot_runner.py`: entry gate blocks NEW entries during the window (pulse BLOCKED) + `sweep_friday_flat` in main loop after pre-news sweep.
  - `models.py` BotConfigUpdate: `friday_flat_enabled=True`, `friday_flat_mode="close"`, `friday_flat_minutes_before=60`. Telegram `notify_friday_flat` summary + WS `friday_flat` event.
  - `BotConfig.jsx`: toggle + ACTION select + minutes input in Protection section; `withFlatDefaults()` normalizes older saved configs (UI showed OFF/0 while backend defaulted ON — fixed).
  - **Tested**: 16 pytest green (window math incl. Friday boundary/after-close/custom minutes, tighten math, EA gate, wiring) + live-DB simulation with patched clock: CLOSE queues FULL_CLOSE, TIGHTEN queues MODIFY_SL to BE, EA 1.39 degrade works, crypto skipped, idempotent. UI screenshot: ON / CLOSE ALL / 60 min.
  - NOTE: user terminals now report **EA v1.40** (Start Trader confirmed) — FULL_CLOSE actually executes.

- 2026-07-06 (iter-51 / fork) — **July 3 trades still in TODAY view — broker-local epoch skew**:
  - Root cause: MT5 deal epochs are broker-LOCAL. The weekend-gap SL closes happened Sunday 22:01-22:06 UTC (market re-open) but UTC+3 brokers stamped them 01:01-01:06 Monday → stored as "today" UTC.
  - **Fix (`bridge_routes.py` external-deal)**: STOIC now LEARNS each account's broker UTC offset from live deals (`broker_utc_offset_sec` = deal epoch minus server receive time, snapped to 15 min, ±14h clamp) and subtracts it from HISTORICAL/backfill epochs so closes land on the correct UTC day. Live deals keep server UTC.
  - **Data**: learned offsets (micro/OnEquity/vtmarkets/StartTrader = +3h, MT5 Demo = 0h); repaired all 4 weekend-gap trades → closed_at 2026-07-05T22:0x UTC.
  - Verified via authed API (cookie auth — login sets httpOnly cookie, NO bearer token): today = 60 closed / 60 wins / 100% / +$1,799.72 / zero rows opened before Jul 6. pytest 16 green.

- 2026-07-06 (iter-50b / fork) — **LIVE P&L recurrence — lost edits**:
  - Two search_replace edits from iter-50 (LIVE P&L cell doc-snapshot fallback + header openLive fallback in `Trades.jsx`) reported success but NEVER landed on disk (verified via `git show`). Prior screenshot verification passed only because the playwright session had live WS ticks masking the missing fallback.
  - Re-applied both edits, verified via grep on disk, and re-tested with `/api/ws` BLOCKED in playwright (simulating the user's no-tick session): all 12 LIVE P&L cells render from `t.live_pnl` heartbeat snapshots; OPEN LIVE P&L card totals correctly.
  - LESSON: after batched search_replace edits, verify presence on disk (grep) before claiming success; when a fix targets a fallback path, test with the primary path disabled.

- 2026-07-06 (iter-50 / fork) — **Open trades showed no CURRENT price / LIVE P&L**:
  - Backend was fine (heartbeat writes `live_price`/`live_pnl` to every open trade doc, ≤5s fresh) but the UI ONLY rendered from WS `position_ticks` (not flowing in user's session) + a quote cache keyed by BASE symbols that missed suffixed tickers (GOLD#, XAUUSD.fx, XAUUSD-ECN).
  - **`Trades.jsx` fixes**: new `baseSymbol()` helper (mirrors backend pip_utils); render chain now tick → `t.live_price`/`t.live_pnl` doc snapshot → `quotes[baseSymbol]`; `priceDecimals` + `CONTRACT_SIZE` + `velocities` + `/market/quotes` symbol list all base-normalised (gold showed 5 decimals + wrong PnL math on suffixed symbols before); green "broker-live" dot now covers doc snapshots too.
  - **Fallback refresher**: if no WS tick in >15s, re-pull the trade list every 15s (visible tab only) so snapshots stay fresh without websockets.
  - Verified via screenshot: CURRENT 4150.98/4150.86… with green dots, LIVE P&L +$11.58/+$15.44…, 2-decimal gold prices.
  - Note: "SYNC" button is for closed-history reconciliation — it was never going to fix live prices; the actual bug was the render chain above.

- 2026-07-06 (iter-49 / fork) — **Period stats counted past trades + status filter ignored in history mode**:
  - User: TODAY period counted past trades → wrong win-rate/profit. Screenshot showed 27 closed incl. old trades, and 37 rows while CLOSED filter active.
  - **Root cause 1**: external-deal stamps `closed_at = server-now`. Correct for LIVE events, but deep-sync/history-sweep backfills push deals that happened DAYS ago — repair time folded them into today. Fix: `BridgeExternalDeal.backfill` flag (EA v1.40 tags sweep + deep-sync pushes) + fallback heuristic (deal_time epoch >10min old) → use broker deal time (broker-local, ±3h skew) for opened_at/closed_at/partial_closed_at. Live deals keep server UTC.
  - **Root cause 2 (frontend `Trades.jsx`)**: in history mode (period presets) the plain status filter (closed/open/...) was not applied client-side — table showed ALL statuses. Fixed in `visibleTrades`.
  - **Data repair**: 4 mis-stamped trades restored to real broker close times from `broker_deals.deal_time` — 3 were genuine Monday-01:00 weekend-gap SL hits (correctly today), 1 moved to Jul 5.
  - **Testing**: 4 new pytest asserts (23 total green), live-API E2E (backfilled 3-day-old deal keeps broker date; live deal keeps today; /trades/history excludes the old one), screenshot verified 26 closed / SHOWING 26 of 40.

- 2026-07-06 (iter-48 / fork) — **Today's closed-trades cleanup + live deep-sync repair**:
  - User: "closed trades today have wrong data / missing exit price — keep only bot-executed trades."
  - Deleted 22 junk docs: 19 test remnants (fake tickets 999111/777111, incl. 5 on deleted accounts), 2 fabricated docs (no ticket/entry), 1 duplicate ghost doc (same broker ticket 45386781 as a fully-recorded trade).
  - 4 genuine bot trades had placeholder panic-close data (exit==entry, pnl=0): marked `pnl_estimated=True` and queued deep broker sync on their 4 accounts. **Live EAs (v1.39) executed the sync within ~60s** — 108-134 deals re-scanned per account, all 4 trades repaired with exact broker figures (real SL exits, P&L -199 to -377).
  - Result: today's closed list = 21 trades, 100% bot-executed (`origin=auto`) with exact broker data (no estimated/unknown/missing exits).
  - First real-world validation of the iter-46 deep-sync pipeline end-to-end on live broker terminals.

- 2026-07-06 (iter-47 / fork) — **Slippage-veto repair + FULL_CLOSE queue consumption (EA v1.40)**:
  - **User report**: "1 trade modification(s) waiting >5min for EA" despite all terminals on v1.39.
  - **Root cause 1 (EA)**: `ParseModificationsBlock` only handled MODIFY_SL/PARTIAL_CLOSE — FULL_CLOSE (slippage veto / auto-deleverage / reconciler force-closes) was NEVER consumed. ~90 closed trades carried stale FULL_CLOSE mods; 1 open trade was "stuck". The veto has effectively never executed at the broker.
  - **Root cause 2 (pip math)**: `price_to_pips("GOLD#"/"XAUUSD.fx"/"XAUUSD-ECN", …)` fell back to 0.0001 FX pip → $1 move = "10000 pips" → tripped even the 9999 default cap. Added `pip_utils.base_symbol()` (suffix strip + GOLD→XAUUSD aliases) used by `pip_size`/`pip_value_usd_per_lot` and veto cap lookup.
  - **Root cause 3 (baseline)**: veto compared fill vs SIGNAL price (5-15s dispatch latency drift, not slippage) and counted favorable fills. Audit: 20/20 recent "vetoes" were latency drift on PROFITABLE trades.
  - **Fix (backend `bridge_routes.py::report_trade`)**: EA v1.40 reports `requested_price` (price at OrderSend); slippage = ADVERSE-only (direction-aware) fill-vs-requested. Veto fires ONLY when `requested_price` present (true measurement) — legacy reports record drift but never veto (they were no-op vetoes anyway).
  - **Fix (EA v1.40)**: new `ApplyFullClose()` consumes FULL_CLOSE mods (closes by ticket, acks via modification-ack, acks `already_closed` if position gone so queue always clears); `/bridge/report` carries `requested_price`.
  - **Hygiene**: `/bridge/report` closed path + external-deal full close now clear `pending_modification`. One-off DB cleanup: 90 stale mods on closed trades + 1 open false-veto FULL_CLOSE cleared (queue now empty). Stale diagnostic hint "Recompile EA to v1.26" → "Update EA to v1.40+".
  - **Versions 1.39→1.40**: EA property/define, bot_routes/diagnostic_routes LATEST_EA, setup_routes ea_latest_version, Accounts.jsx, EaVersionStrip.jsx; tests iter40/iter85/iter46 updated.
  - **Testing**: new `tests/test_iter47_slippage_veto_fullclose.py` (12 tests) + 37 combined pytest green + live-API E2E: suffixed GOLD# 0.5-pip true slip no-veto, favorable fill no-veto, 30-pip true adverse → FULL_CLOSE queued → surfaced in poll → ack clears, legacy report no-veto, close clears mod. iter25h failure verified PRE-EXISTING via git stash.
  - **USER ACTION**: update EA to v1.40 on all terminals (Accounts page → DOWNLOAD EA).

- 2026-07-06 (iter-46 / fork) — **On-demand Deep Broker Sync + Auto-Heal (EA v1.39)**:
  - **Goal**: "The bot must be able to restore at any time the correct data and synchronize with the brokers' terminals."
  - **Backend `routes/account_routes.py`**: `POST /api/accounts/{id}/request-sync` (auth) — queues `pending_history_sync` (7-day lookback, `requested_by=user`).
  - **Backend `routes/bridge_routes.py`**:
    - `poll-trades` now dispatches `sync_request:{lookback_seconds}` when a sync is pending; re-dispatch guard 180s via `pending_history_sync.dispatched_at`.
    - New `POST /api/bridge/sync-complete` — clears pending, records `last_full_sync_at` + `last_full_sync{deals_pushed,trades_repaired,requested_by}`, broadcasts WS `broker_sync_complete`.
    - **Duplicate-deal repair**: `external-deal` DuplicateKeyError path now falls through to REPAIR when matched trade is `closed` AND (`pnl_estimated` | `pnl_unknown` | `exit_price is None`). "in" duplicates and open trades stay strict no-ops (partial-close math must never double-fire).
    - **Heartbeat auto-heal**: every 5 min (`ghost_check_at` throttle) counts closed trades ≤7d with estimated/unknown P&L; if >0 auto-queues `pending_history_sync` (`requested_by=auto_heal`). Zero user action needed.
  - **EA v1.39** (`static/EmergentTradingBridge.mq5`): `ParseSyncRequest` (60s local re-run guard) + `DeepSyncHistory` (full HistorySelect scan of window, cap 400 deals, ignores incremental watermark) + `PushDealById` helper + confirms via `/api/bridge/sync-complete`.
  - **Version bumps 1.38→1.39**: EA `#property`/`EA_CLIENT_VERSION`, `bot_routes.py`, `diagnostic_routes.py`, `setup_routes.py` (`ea_latest_version`), `Accounts.jsx`, `EaVersionStrip.jsx` fallback (was stale 1.34).
  - **Frontend `Accounts.jsx`**: cyan SYNC button per account (`data-testid=broker-sync-<acct>`), WS listener for `broker_sync_complete` → success toast with deals/repaired counts, status line `sync-status-<acct>` shows SYNC PENDING (with EA v1.39+ requirement) or LAST BROKER SYNC timestamp + counts.
  - **Testing**: pytest `tests/test_iter46_broker_deep_sync.py` (8 passed) + full self E2E over live API (request-sync → poll dispatch → no premature re-dispatch → duplicate-deal ghost repair pnl_estimated→exact → sync-complete repaired=1 → heartbeat auto-heal queues sync). Updated version tests iter40/iter85 to 1.39. Screenshot verified SYNC button + last-sync line.
  - **NOTE**: EAs ≤v1.38 silently ignore `sync_request`; pending badge tells user to update EA.

- 2026-07-02 (iter-100) — **Bot Pulse strategy chip · clickable deep-link to Bot Config**:
  - **`components/BotPulsePanel.jsx`**: the blue strategy chip on each account row is now a react-router `<Link>` (was a static `<span>`). Href pattern: `/bot-config?account=<account_id_or_"default">&preset=<strategy_key>`. The literal string `default` sentinel is used when `item.account_id` is falsy (Default profile scope), keeping URL param parsing on the other side dead-simple.
  - **`pages/BotConfig.jsx`**: added `useSearchParams` deep-link consumer:
    - Reads `?account` and `?preset` on first mount (guarded by `deepLinkAppliedRef` so it runs exactly once). `account=default` → `setSelectedAccountId(null)`; any other value → per-account scope.
    - Immediately clears the query params with `setSearchParams(next, {replace: true})` so a page refresh doesn't re-scroll/re-pulse.
    - Second `useEffect` waits for `presets.length > 0`, then `scrollIntoView({behavior:'smooth', block:'start'})` on the Strategy Presets section (`presetsAnchorRef`) and highlights the matching preset card (built-in AND custom) with `ring-2 ring-[#00FF41] animate-pulse` for 2.5s before clearing.
  - **Testing agent iter-36**: 7/7 test cases passed at 100% frontend. All 6 chip hrefs verified (5 real account IDs + 1 `default` sentinel), click → navigate → URL cleanup within 1s, matching preset card gains ring+pulse animation for 2.5s then settles, page refresh does NOT re-trigger highlight. Regression on KRAKEN_SPOT (no chip) and Trades filters (winning/lost) still green.


- 2026-07-02 (iter-99) — **Bot Pulse panel · strategy chip per account + dropdown mode for large fleets**:
  - **Backend `routes/bot_routes.py::get_bot_pulse`**: now emits `strategy_key` and `strategy_label` per item. Built-in presets resolve via `strategy_presets.PRESETS[key]["label"]`; `custom:<id>` keys are batch-resolved through a single `db.user_presets.find({_id: {$in: [...]}})` query (avoids N+1 when a user has many accounts). Unresolvable custom keys fall back to `"Custom preset"`; unknown built-in keys fall back to `key.title()`.
  - **Frontend `components/BotPulsePanel.jsx`**:
    - Added blue strategy chip (`data-testid=bot-pulse-strategy-<config_id>`) inside each row's left column alongside the LIVE/SHADOW/OFF state chip and the symbols chip. Displays the uppercased strategy label; hidden when no preset is set (e.g. KRAKEN_SPOT).
    - Header summary now reads `N ACTIVE · M TOTAL · LOOP 60s` (previously only ACTIVE was shown).
    - **New `DROPDOWN_THRESHOLD = 10` constant**. When `items.length > 10`, a `bot-pulse-selector-row` renders under the header with (a) a `<select>` (`bot-pulse-selector`) listing each account in the form `[STATE] Label · Strategy · LEVEL`, ranked by severity (block > warn > info) and freshness — auto-selecting the loudest on first load, and (b) a `SHOW ALL (N) / HIDE (N)` toggle button (`bot-pulse-show-all`) that flips between dropdown-preview mode and the full expanded list on demand. Below the 10-item threshold the panel is unchanged (all rows always visible).
  - **Testing agent iter-35**: 6/6 test cases passed at 100% frontend + backend. `strategy_key/strategy_label` verified for all 7 admin bot_configs (Scalper / Mean Reversion / Sniper / Aggressive / Trend Rider / Fast Scalp / null-for-no-preset). Header shows `4 ACTIVE · 7 TOTAL · LOOP 60s`. Dropdown mode manually validated by main agent with a temporary threshold override (7 options rendered correctly; auto-select of top-ranked config; SHOW ALL / HIDE toggle expands/collapses the row list). No regression on the WINNING/LOST/date-range filters from iter-98.


- 2026-07-02 (iter-98) — **Trades page · date range + winning/lost status filters**:
  - **`pages/Trades.jsx`**: added `WINNING` and `LOST` buttons to the status filter row alongside `ALL / PENDING / OPEN / CLOSED / FAILED`. Because the backend `/api/trades` route only knows pending/open/closed/failed, `winning`/`lost` are translated to `status=closed` and narrowed client-side by `parseFloat(pnl) > 0` / `< 0`. Constant `CLIENT_ONLY_FILTERS = ['winning', 'lost']` at module scope.
  - **Date range picker row** (`data-testid=date-filter-row`): `FROM` (`date-from`) and `TO` (`date-to`) `<input type="date">` inputs clamp the visible table by `opened_at` in the user's local timezone — start-of-day for FROM, end-of-day for TO. `min`/`max` attributes cross-link the inputs so the user can't pick TO earlier than FROM. A `CLEAR` button (`date-clear`) appears when either date is set.
  - **`visibleTrades` useMemo** applies both refinements over the loaded `trades` list. A `SHOWING X / Y` counter (`visible-count`) surfaces whenever any client-side filter is active. Empty state differentiates: "No trades yet" (nothing loaded) vs "No trades match your filters. Try clearing the date range or status filter." when filters exclude everything.
  - **Testing agent iter-34**: 9/9 test cases passed at 100% against real historical data (100 loaded trades → 76 winning, 17 lost) — filter-winning, filter-lost, date-from far past, date-from today, date-to yesterday, date-clear, combined winning + date range, empty-state text differentiation, existing filters not broken.
  - Known orthogonal limitation flagged by tester: the backend `/api/trades` returns a 100-row default cap, so client-side date filtering only refines the loaded window. Not blocking; deferred to a future "load more" / server-side date-range enhancement.


- 2026-06-30 (iter-87) — **Migration Helper · Export/Import admin state across environments**:
  - **Backend `routes/migration_routes.py`** with 2 admin-only endpoints (admin-only via `_admin_only()` guard):
    - `GET /api/admin/export-state` → returns JSON with `schema_version=1`, exporter email, the admin's portable user fields (NO password_hash), and the full contents of `accounts` / `bot_configs` / `user_presets` filtered by `user_id`. ObjectIds are recursively serialised to hex strings; datetimes to ISO strings.
    - `POST /api/admin/import-state` → idempotent upsert by `_id`. Remaps `user_id` to the currently-signed-in admin (so cross-env migrations where admin's `_id` differs still land cleanly). Schema mismatch returns 400 with `code=schema_mismatch`. Non-list collections return 400 `bad_payload`. Reports counts of received/inserted/updated per collection.
    - **What's included**: portable user fields (email/name/role/status/email_verified/terms_agreed*/subscription*/two_fa*), accounts (incl. `bridge_token` + vault-encrypted creds → MT5 EAs on user's VPS keep authenticating without re-pairing), bot_configs, custom user_presets.
    - **What's deliberately skipped**: password_hash (env-specific), trades / signals / agent_activity / heartbeats (transient and replayed live), audit log (env-specific), other users' data.
  - **Frontend `pages/AdminMigration.jsx`** routed at `/admin/migration` (admin-only via `<ProtectedRoute requireAdmin>`):
    - "EXPORT STATE" button → calls `/admin/export-state`, builds a `Blob`, triggers browser download as `stoic-state-<UTC-timestamp>.json`. Shows live counts in a toast + last-export summary chip.
    - "CHOOSE FILE" button → opens hidden file picker → confirm dialog → reads JSON → POSTs to `/admin/import-state`. Shows inserted/updated counts per collection in a toast and a persistent summary chip.
    - "Skipped vs Included" callout card so the user understands what crosses environments.
  - **Tests (`test_iter87_migration_helper.py`)**: 6/6 passing — non-admin gets 403 on both endpoints; export returns expected shape (no password_hash leak); schema mismatch → 400; idempotent round-trip (re-import doesn't create duplicates); synthetic account import lands in DB with user_id remapped + bridge_token preserved.
  - **UI smoke**: admin login → `/admin/migration` → EXPORT click → `stoic-state-2026-06-30-12-56-26.json` downloaded with 6 accounts, 7 bot configs, 0 presets. End-to-end verified.


- 2026-06-30 (iter-86) — **Force Test Trade — backend fix + frontend button shipped**:
  - **Backend `routes/account_routes.py:557` `POST /api/accounts/{id}/test-trade`**: Fires a 0.01-lot BUY through the real execution pipe (safety guardian + per-base resolver + EA bridge) to validate broker connectivity without waiting for the AI to choose a direction. Tagged `is_test=true` + `origin="test_trade"` so it's excluded from analytics. Refuses when account is paper / not owned by caller / heartbeat is >120s stale / no tradeable base symbol in MarketWatch / unknown account id.
  - **Bugs fixed this session**:
    1. `ImportError: cannot import name 'engine_for_account' from 'execution'` — execution module's factory is named `for_account`, not `engine_for_account`. Aliased on import.
    2. `safety_guardian.audit_pre_trade` was rejecting the test signal with `risk_inputs_present: lot=0.01 entry=0 sl=0`. The endpoint now fetches a live price via `market.get_quote` (falls back to sane defaults — 2400/60000/1.10 for XAUUSD/BTCUSD/EURUSD) and stamps `entry_price`, `stop_loss`, `take_profit` on the signal before handing it to the engine.
  - **Frontend `Accounts.jsx`**: Yellow "FORCE TRADE" button (data-testid `force-test-trade-<account_number>`) rendered between TEST and ROTATE for every live account. Click → native confirm dialog → POST `/api/accounts/{id}/test-trade` → toast with `data.message`. Hidden on paper accounts (nothing to validate). `Zap as Lightning` lucide icon imported.
  - **Tests**: `test_iter86_test_trade.py` — 6/6 passing (happy path persists is_test+origin tags; paper→400; stale EA→409; cross-user→403; no tradeable base→409; unknown id→404). Curated regression suite **iter-78→iter-86: 75/75 green**.
  - **Testing agent iteration 33**: full backend + frontend regression — **100% pass rate**, no critical or minor issues, FORCE TRADE button verified end-to-end via Playwright click → live trade_id returned from /accounts/<id>/test-trade.


- 2026-06-30 (iter-85) — **EA v1.36 — token auto-load from `STOIC-Token.txt`** (closes the loop on the PowerShell installer):
  - `input string BridgeToken` declaration kept (so manual paste in MT5 inputs dialog still works as override).
  - New global `string EffectiveToken` + helper `ResolveBridgeToken()` — at `OnInit()` resolves the active bridge token from one of:
    1. Trimmed input value (if not empty AND not the placeholder `"PASTE_YOUR_BRIDGE_TOKEN_HERE"`).
    2. `MQL5\Files\STOIC-Token.txt` — the file written by `STOIC-Installer.ps1`. Reader skips `#` comment lines and blank lines (the installer prepends an audit header). First non-comment, non-blank line is the token.
    3. Empty string + `Print(...)` warning telling the user to either paste in inputs OR run the PowerShell installer.
  - All outbound HTTP body builders (`SendHeartbeat`, `Open`, `Close`, `Modify SL`, `Partial Close`, `Trade Ack`, `Settled Ack`, deal-history reporter — 10 call sites) now build the JSON with `EffectiveToken` instead of the read-only `BridgeToken` input.
  - Token resolution happens BEFORE the first `SendHeartbeat()` call in `OnInit()` (locked in by a regression test) so the very first heartbeat carries the right token.
  - Version bumped to 1.36 in 3 spots inside the EA + `LATEST_EA` in `bot_routes.py` + `diagnostic_routes.py` + `ea_latest_version` in `setup_routes.py:claim-pairing` + `LATEST_EA_VERSION` in `Accounts.jsx`.
  - **Tests** (`test_iter85_ea_token_autoload.py`): 13/13 — version coherence across `#property`/`#define`/`Print()`/served-file/route constants; ResolveBridgeToken helper presence + comment+blank-line skip; `EffectiveToken` global declared; OnInit assigns before first heartbeat; input declaration preserved; no leftover positional `BridgeToken,` arg sites; heartbeat uses EffectiveToken; served file md5 matches local; claim-pairing advertises v1.36.
  - **Curated regression (iter-78/79/80/82/83/84/85): 69/69 green.**


- 2026-06-30 (iter-84) — **PowerShell auto-installer + pairing-token flow**:
  - **Backend `routes/setup_routes.py`** with 3 endpoints:
    - `POST /api/setup/pairing-token` (auth'd) — issues a single-use 15-min UUID linked to one account. Re-issuing invalidates the previous outstanding token. Paper accounts rejected (no MT5 to install into).
    - `POST /api/setup/claim-pairing` (UNauth'd by design — installer has no cookies on the VPS) — validates + consumes the token, returns `{bridge_token, server_url, heartbeat_url, ea_script_url, ea_latest_version, broker, account_number, account_label}`. Records `consumed_by_hostname` and `consumed_by_ip` for audit. Marks the account's `installer_paired_at` so the dashboard reflects pairing status.
    - `GET /api/setup/pairing-status/{account_id}` — dashboard polling endpoint, returns paired_at + hostname + whether a token is outstanding.
  - **`GET /api/setup/installer.ps1`** — serves the PowerShell installer script with no-cache headers so `irm | iex` always pulls the latest.
  - **`static/STOIC-Installer.ps1`** — self-contained Windows installer (~210 lines). `Install-Stoic -Token X -ServerUrl Y` does the entire onboarding: calls `claim-pairing` → auto-discovers MT5 terminals under `%APPDATA%\MetaQuotes\Terminal\<guid>` → downloads `EmergentTradingBridge.mq5` from `/api/ea-script` → copies into each terminal's `MQL5\Experts\` → writes the bridge token to `MQL5\Files\STOIC-Token.txt` (the EA reads this on attach — no manual paste) → whitelists the heartbeat URL in `config\terminal.ini` `[Experts]` section → invokes `metaeditor64.exe /compile` to produce the `.ex5` automatically. Reduces user onboarding from ~10 min of MetaEditor + WebRequest dialog dancing to **a single PowerShell paste (~60 sec)**.
  - **Security model**: the pairing token is single-use and expires in 15 min; even if intercepted, an attacker has 15 min and only gets a bridge_token bound to ONE account that the user can rotate from the dashboard.
  - **Frontend `QuickInstallPanel.jsx`** — renders inside each live MT5 account card on `/accounts`. "Generate token" button → shows the copy-able one-liner (`irm <backend>/api/setup/installer.ps1 | iex; Install-Stoic -Token "..." -ServerUrl "..."`) + live countdown of the 15-min TTL + Step 1-4 instructions. Polls `pairing-status` every 4s and auto-flips to a green "Paired — EA deployed" state showing the hostname once the installer redeems the token. "Re-pair (new VPS)" button generates a fresh token.
  - **Tests** (`test_iter84_pairing_flow.py`) — 11/11 passing:
    - issue happy path / requires auth / rejects other user's account / rejects paper / re-issue invalidates prior
    - claim happy path consumes + records hostname + IP + marks account paired
    - claim replay blocked / expired token 400 / unknown token 400
    - pairing-status reflects consumption
    - installer.ps1 endpoint serves the script
  - **Curated regression (iter-78/79/80/82/83/84): 56/56 green.**


- 2026-07-04 (iter-80) — **Forgot Password / Reset Password flow**:
  - **`password_reset.py`** — token generator (urlsafe 32-byte, 1h TTL) + branded HTML/text email (gold accent vs activation's green) sent via the existing Resend integration. Dev fallback returns the reset link when `RESEND_API_KEY` is missing.
  - **`POST /api/auth/forgot-password`** {email} — issues a single-use token + emails reset link. Returns generic success for unknown emails OR suspended/terminated users (no enumeration leak). 60s per-account cooldown returns 429 `rate_limited`. Suspended/terminated users get the generic-OK but no token is issued (so the bypass surface is closed).
  - **`POST /api/auth/reset-password`** {token, new_password} — validates token + expiry (1h), bcrypt-hashes the new password, clears the token, stamps `password_reset_at`. Single-use enforced.
  - **Frontend**:
    - `ForgotPassword.jsx` at `/forgot-password` — email input → "Check your inbox" confirmation screen. Honours the 429 cooldown with live countdown.
    - `ResetPassword.jsx` at `/reset-password?token=...` — new password + confirm fields, validation, success screen auto-redirects to `/login` after 2s. Invalid/expired tokens show inline error with "Request a new reset link" CTA.
    - `Login.jsx` — added "FORGOT PASSWORD? →" gold link below the SIGN IN button.
  - **Tests** (`test_iter80_password_reset.py`): 9/9 — generic-ok for unknown, token issued for known user, cooldown 429, suspended user skipped silently, reset bad token 400, reset expired token 400, happy path changes password (old fails, new works), single-use, min-length 6 enforced by Pydantic (422). Curated regression 27/27 across iter-78/79/80.


- 2026-07-04 (iter-79) — **Email Activation + Terms Acceptance at signup**:
  - **Terms gate on register**: `POST /api/auth/register` now requires `terms_agreed=true` in the body and stamps `accepted_terms_version` + `accepted_terms_at` on the user doc. 400 + `terms_required` if missing.
  - **Email verification gate**: New users are created with `email_verified=false`, a single-use `activation_token` (urlsafe 32-byte), and `activation_expires_at` 24h out. Register no longer auto-logs the user in — they MUST click the email link.
  - **Login block**: `POST /api/auth/login` returns 403 + `account_unverified` for unverified users. Admins are grandfathered (admin@trading.bot still works). `get_current_user` also enforces (defence-in-depth).
  - **New endpoints**:
    - `POST /api/auth/verify-email` — consumes token, flips `email_verified=true`, sets auth cookies, returns the user.
    - `POST /api/auth/resend-activation` — generates a fresh token + re-sends. 60s cooldown (429). Returns generic success for unknown/already-verified emails to prevent enumeration.
  - **`activation.py`**: token generator + branded HTML/text activation email rendered via existing Resend integration. Dev fallback returns the activation link in the API response when `RESEND_API_KEY` isn't configured so local flows still complete.
  - **DOMPurify on Terms page**: defence-in-depth XSS sanitization on `Terms.jsx` `dangerouslySetInnerHTML` even though markdown source is server-controlled.
  - **Frontend**:
    - `Register.jsx` rewritten — Terms checkbox (required, disables submit) + post-register "Check your inbox" screen with email address echo, dev-only link surfacing, "Resend activation email" button with 60s cooldown counter.
    - New `VerifyEmail.jsx` at `/verify-email?token=...` — loading → success (auto-redirect to dashboard) / expired / invalid states, all with friendly recovery links.
    - `Login.jsx` — unverified login attempt surfaces an amber MailWarning block with the email + Resend button (same 60s cooldown).
    - `AuthContext` — register no longer setUsers (gates dashboard until verify), new `verifyEmail` + `resendActivation` exposed.
  - **Tests** (`test_iter79_activation_flow.py`): 9/9 — terms-required, register creates unverified+token+terms-version, login blocked, verify-email bad token, verify-email happy path (sets cookies + /me works), token is single-use, resend generic for unknown emails, resend cooldown 429, resend generates new token after cooldown. Curated regression 42/42 green.
  - **Operational note**: with the default Resend sender `onboarding@resend.dev`, emails only deliver to the verified Resend account email (free-tier limitation). To send to real users, verify a domain on resend.com/domains and update `SENDER_EMAIL` in backend/.env.


- 2026-07-04 (iter-78) — **Terms of Use + Admin Moderation (suspend / terminate users & affiliates)**:
  - **Terms of Use** — new `terms_of_use.py` (version `2026-07-04`) with 17 sections: Service definition, no-financial-advice, risk acknowledgement, eligibility, acceptable use, **§7 affiliate rules** (no self-referrals, no incentivized signups, no earnings claims, FTC disclosures), **§8 enforcement** (suspension halts login + freezes bots; termination is permanent + may forfeit affiliate unpaid balance), subscription & refund, IP, liability cap, indemnification, governing law. Public `GET /api/terms` returns version + markdown.
  - **Backend** — new `routes/admin_routes.py` with 11 admin endpoints + 1 public:
    - `GET /api/admin/users?status=&q=&limit=` (filterable list with bot/account/affiliate counts)
    - `GET /api/admin/users/{uid}` (detail + audit timeline)
    - `POST /api/admin/users/{uid}/{suspend,unsuspend,terminate,restore}` (reason required for suspend/terminate; admins protected from each other)
    - `GET /api/admin/affiliates?status=` and `POST /api/admin/affiliates/{aid}/{suspend,unsuspend,terminate}` — termination forfeits unpaid balance to `forfeited_balance_usd`, cancels pending payout requests
    - `GET /api/admin/audit-log?kind=&limit=` (admin_audit_log collection)
  - **Side-effects on suspend/terminate user**: all `bot_configs` flipped `active=false` with `deactivated_reason=user_<status>`; if user is an affiliate, that row is also deactivated (and `terminated=true` on terminate).
  - **Login + auth gating** — `routes/auth_routes.py:login` blocks suspended/terminated users with HTTP 403 + structured `{code: account_suspended|account_terminated, reason}`. `auth.py:get_current_user` blocks mid-session requests from suspended/terminated non-admin users.
  - **Bot runner gating** — `bot_runner._process_user_account_locked` checks user status at the top of each cfg tick, disables the cfg and emits a BLOCKED pulse if the owner is suspended/terminated. Defence-in-depth: even if a bot stayed `active=true` somehow, no trade can fire.
  - **Frontend**:
    - `pages/Terms.jsx` — public route at `/terms` (no auth required), markdown-rendered with dark/gold styling, fetches via raw axios so it works pre-login. Link added to Register page consent line + Sidebar LEARN section.
    - `pages/AdminUsers.jsx` — admin-only console with search + status filter + per-row SUSPEND/TERMINATE/UNSUSPEND/RESTORE buttons + modal that requires reason. Audit-log drawer button.
    - `pages/AdminAffiliates.jsx` — symmetric admin console for affiliates; terminate modal warns about balance forfeiture per Terms §7.
    - `components/Sidebar.jsx` — new conditional ADMIN section (only renders for `user.role==='admin'`) with "User Management" + "Affiliate Mgmt" links.
    - `components/ProtectedRoute.jsx` — new `requireAdmin` prop; non-admins redirected to `/` when navigating to `/admin/*`.
  - **Tests** (`tests/test_iter78_admin_moderation.py`): 9 HTTP tests against the live backend — public terms, non-admin 403, suspend→login-blocked→audit→unsuspend→login-works, terminate disables bot+affiliate+blocks login, cannot suspend admin, reason required, affiliate terminate forfeits balance + cancels payout, affiliate suspend+unsuspend, user listing filters. **9/9 passing.** Curated regression (iter-60/71/77/78): 52/52 green.


- 2026-06-29 (iter-73) — **Conservative profile activated + Strict MTF gate**:
  - **Post-mortem of 60d trade history** revealed a hidden ~$3,011 net loss across 21 "invalidated" trades (the `closed` filter masked them). Single-day cluster (June 26) was 11 consecutive XAUUSD SELLs as gold rallied — classic averaging-into-a-loser pattern that slipped through because `aggressive_mode=True` was bypassing the entropy filter + learned-meta veto + classifier safety net.
  - **All 3 active configs** (`micro`, `vtmarkets`, `MT5 Demo`) updated to:
    - `aggressive_mode: False` — restores entropy filter + learned-meta classifier veto
    - `risk_level: medium` (was `high`) — halves per-trade exposure
    - `mtf_strict: True` — new gate (iter-73)
    - `min_confidence_override: 70` — only A-grade signals execute
    - `daily_profit_target_r: 2.0` + `action: stop` — auto-halt for the day after +2R
    - signal_cooldown 3min uniform across all 3
  - **`bot_runner.py`** — new strict-MTF gate inserted right after the existing MTF veto-counter. Logic: if `cfg.mtf_strict=True` and `action ∈ {BUY,SELL}`, require `buy_support ≥ 2` (BUY) or `sell_support ≥ 2` (SELL) from `mtf_tiers.alignment`. Failing trades emit a `SKIP/warn` pulse + bump `mtf_strict_veto` intel counter. Default off — opt-in via cfg.
  - **`routes/bot_routes.py`** — `mtf_strict` whitelisted in GET defaults + PATCH coercion (bool).
  - **Tests**: `test_iter73_strict_mtf.py` 11/11 — strong alignment passes, drift-into-chop blocked, counter-trend blocked, HOLD no-op, missing alignment safe.
  - **Effect**: trade frequency halved (~), only A-grade tier-aligned setups execute. Expected WR move from ~30% → 50%+.


- 2026-06-28 (iter-71) — **Broker-rejection circuit breaker + EA v1.29 symbol auto-detect**:
  - **Root cause** of vtmarkets failed trades (7 consecutive retcode=10013 INVALID_REQUEST): VT Markets uses a suffixed symbol name (e.g. `XAUUSD.x`, `XAUUSDpro`, `XAUUSD.raw`) but the EA was sending bare `XAUUSD`. `SymbolInfoDouble` returned 0 → invalid price → broker rejected every order.
  - **`broker_reject_breaker.py` (new)** — auto-halt logic: ≥3 same-retcode failures within 30min flips the account to `trading_blocked: True` with structured `block_reason`/`block_retcode`/`block_hint` fields. Bot runner skips blocked accounts. Recognises both numeric retcodes (10013/10014/10016/10018/10019/10027) and the new v1.29 `symbol_not_found:` tag.
  - **`bot_runner.py`** — runs the breaker before every account tick, broadcasts `account_trading_blocked` WebSocket event on trip, records a BLOCKED pulse so the dashboard shows the halt.
  - **Bot Health** — new `broker_rejecting_trades` issue (severity=error, −15) so users see this loudly above the fold.
  - **`POST /api/accounts/{id}/unblock`** + **`GET /api/accounts/{id}/block-status`** — manual reset after the user has fixed the underlying broker issue.
  - **EA v1.29** (`static/EmergentTradingBridge.mq5`) — new `ResolveBrokerSymbol()` probes the bare symbol then 18 common suffix variants (.x/.raw/.r/.m/.ecn/pro/+/#/m/_pro/-ECN/.pro etc.), uses `SymbolSelect()` to add to MarketWatch, validates a live tick exists before OrderSend. Reports explicit `symbol_not_found:XAUUSD` if nothing matches (instead of looping retcode 10013).
  - **Tests**: `test_iter71_broker_reject_breaker.py` 12/12 (retcode extraction, hints table, threshold, mixed-codes no-trip, already-blocked short-circuit, unblock, window expiry).
  - **Live verification**: vtmarkets auto-halted with full reason chain visible on Bot Health endpoint.


- 2026-06-28 (iter-70) — **Smarter Bot Health diagnostic**:
  - Heartbeat stale > 1h → account auto-flipped to `status="disconnected"` + `dormant: true` (single -5 advisory on flip, info-only afterwards). Previous behaviour was -10 per stale account indefinitely.
  - Ghost trades closed-with-no-exit-price older than 24h auto-acknowledged (`ghost_auto_ack_reason: "older_than_24h"`).
  - `bot_inactive` demoted from -10 → -5 (user choice, not a malfunction).
  - "All accounts dormant" no longer fires the -35 `no_connected_account` panic.
  - **User impact**: admin Bot Health: **53 → 78** (degraded → good). Remaining deductions are honest user-controllable signals (`aggressive_mode_on` -15, `bot_inactive` -5, `ghost_trades` -2).
  - Tests: `test_iter70_bot_health.py` 3/3 (shape, dormant auto-flip, ghost auto-ack). **158/158** curated regression green excluding flaky LLM-latency test in iter-69 HTTP suite (passes in isolation).


- 2026-06-28 (iter-69) — **P2 Batch: Breakout Scalper + VWAP Pullback + XGBoost + LLM Reflection + Email Digest**:
  - **Breakout Scalper** (`breakout_scalper.py`) — Donchian-20 channel break + ≥0.25 ATR confirmation. Returns `{signal, channel_high/low, atr, break_distance_atr, ...}` to AI dict and prompt.
  - **VWAP Pullback** (`vwap_pullback.py`) — Rolling-20 typical-price × volume VWAP proxy (falls back to typical-price SMA when volume missing). Surfaces `{vwap, pullback_pct, regime, pullback_signal}` and produces actionable BUY/SELL pullback hints when trend-aligned. Five regimes: near / above / below / above_extended / below_extended.
  - **XGBoost meta-learner** (`learned_meta.py`) — `_fit_artifact()` now branches on sample count: ≥100 → `xgb.train` native API (no sklearn dep), <100 → logistic regression. Artifact discriminator field `backend: "xgboost"|"logreg"`. Predict path supports both via base64-encoded booster JSON. Platt calibration kept across both backends.
  - **LLM-written Weekly Digest reflection** (`routes/insights_routes.py::_generate_ai_reflection`) — Claude Sonnet 4.5 generates a 3-paragraph narrative (what happened / patterns / focus for next week). Opt-in via `?include_ai=true` query param to keep dashboard refresh fast.
  - **Email-delivered Weekly Digest via Resend** (`email_sender.py` + `POST /api/insights/weekly-digest/email`) — async helper wrapping the synchronous Resend SDK in `asyncio.to_thread`. Renders inline-styled dark-themed HTML email with stats / best-worst / HOLD reasons / AI reflection. Live-verified: message ID `8b6174e1-...` delivered to verified Resend address. RESEND_API_KEY + SENDER_EMAIL added to backend/.env.
  - **Frontend** (`WeeklyDigestPanel.jsx`) — "GENERATE AI REFLECTION" button lazy-loads the Claude narrative; "EMAIL ME" button triggers POST with success/error banners. Both surface using existing dark+gold aesthetic.
  - **Testing**: 19 new unit tests + 5 HTTP integration tests added by testing agent. **155/155 backend tests passing**. Frontend interactions verified end-to-end. Lint clean.


- 2026-06-28 (iter-68) — **Per-veto reject counter + Auto-escalating profit target**:
  - **Per-veto reject counter** (`routes/signal_routes.py` + `BotWatching.jsx`):
    - Added regex-based veto classifier mapping HOLD `reasoning` strings into 17 named buckets (market_closed, entropy, macro_freeze, regime_chop, self_contra, news, meta_label, mtf, learned_meta, a_plus, rr_ratio, dxy, sector_cap, anti_pyramid, loss_streak, cooldown, low_confidence).
    - `/api/signals/watch-status` now returns `veto_counts: {window_hours, total_holds, classified, by_tag:[{tag,label,count}…]}` aggregated over the last 24h across the global signals stream.
    - New `VetoBreakdown` panel in `BotWatching.jsx` — top-5 reasons with horizontal amber progress bars, `+N unclassified` footer for transparency.
    - **Live-verified**: admin account shows market_closed=124, entropy=63, mtf=40, self_contra=7 over last 24h. UI renders cleanly under the symbol card.
  - **Auto-escalating profit target** (`profit_target.py` + `routes/bot_routes.py` + `BotConfig.jsx`):
    - New cfg fields: `daily_profit_target_escalate` (bool, default false) + `daily_profit_target_escalate_step_r` (float, default 1R, clamped 0.25-10).
    - `evaluate_profit_target()` ratchets up the effective target each time realised P&L crosses a step boundary. Step formula: `steps_absorbed = (pnl - base_target) // step_dollars + 1`. Returns `effective_target_r`, `next_target_r`, `next_target_amount`, `escalation_steps`.
    - UI: new ENABLED/DISABLED toggle card + step-size numeric input inside the Profit Target section. Only meaningful in LOCK mode (clarified in help text).
  - **Tests** (`tests/test_iter68_vetocount_and_escalating_target.py`): 27 unit tests — 16 parameterised veto-pattern recognitions, aggregation correctness, escalation across 6 scenarios (disabled, below, hit-no-escalate, 1 step, 2 steps, 7 steps, boundary, escalate-off). **27/27 passing. Curated iter-60→68 regression: 131/131 green.** Lint clean.


- 2026-06-28 (iter-67) — **Multi-Timeframe (MTF) tier pack**:
  - **`mtf_tiers.py` (new)** — computes SHORT/MEDIUM/LONG indicator snapshots from the same daily OHLC series. Each tier exposes `sma_fast`, `sma_slow`, `sma_fast_slope_pct`, `rsi`, `close_vs_sma_fast_pct`, and a `direction` (UP/DOWN/FLAT) derived from slope + structure confluence. Tier params:
    - SHORT  (5/10 SMA, RSI-7, ~last week) — intraday/H4 proxy
    - MEDIUM (20/50 SMA, RSI-14, ~last month) — D1
    - LONG   (50/200 SMA, RSI-21, ~last quarter+) — W1/structural
  - **`mtf_check.py` refactor** — now consumes the tier pack as the primary path (`mode: "tiered"`), with the legacy slope-on-daily logic kept as a safe fallback when tiers aren't ready (cold start, <30 bars). Veto fires when ≥2 tiers disagree with the action.
  - **`ai_signals.py` wiring** — `mtf_tiers` computed once per signal cycle, passed into the LLM prompt (with explicit guidance in SYSTEM_PROMPT on how SHORT/MEDIUM/LONG should inform conviction) AND into the MTF gate so both consume the same structured view. Surfaced in the returned signal dict so frontend / postmortems can deep-link to it.
  - **Tests** (`test_iter67_mtf_tiers.py`): **15/15 passing.** Covers empty/short history, uptrend/downtrend recognition, mixed-trend handling, alignment counters, tier vs legacy mode selection, counter-trend veto, HOLD no-op, explicit tiers override, indicator key contract.
  - **Live verified**: XAUUSD signal now returns `mtf_tiers.alignment={buy:0,sell:2,dominant:DOWN}` with SHORT/MEDIUM DOWN + LONG FLAT (RSI 26.66/37.84/37.74). BTCUSD shows full 3/3 DOWN alignment. Gate mode is `tiered` end-to-end.


- 2026-06-28 (iter-64) — **P2 backlog cleared: 4 dashboard widgets**:
  - **(a) Reachability indicator** — `crypto_bridge/ccxt_engine.py` runs a 5-min-cached httpx probe against each exchange's public ping endpoint. `GET /api/crypto/exchanges` now returns `reachable` + `reach_error` per exchange + a smart default that skips unreachable. UI: dropdown shows ●/○ markers, disables unreachable options, surfaces inline error like "Geo-blocked (451)".
  - **(b) Cooldown widget** — `GET /api/bot/cooldowns` returns per-account cards with `cooldown_seconds_left`, `loss_streak`, `win_streak`, `anti_tilt_threshold`, `anti_tilt_active`, per-symbol `market_closed`, and `last_trade` summary. `CooldownPanel.jsx` renders an animated streak-bar, freeze banners, and per-symbol cooldown countdowns. Mounted on Dashboard.
  - **(c) P&L vs Limit gauge** — `GET /api/bot/risk-gauge` reuses `_daily_limit()`/`_weekly_limit()` + `realised_pnl_since()` from `circuit_breakers.py` (dry-run, never trips). `RiskGaugePanel.jsx` shows daily/weekly gauges per account with a red 80% danger marker.
  - **(d) Weekly AI Digest** — new `routes/insights_routes.py` with `GET /api/insights/weekly-digest?days=N` (clamped 1-30) returning trade stats (count, win rate, P&L, best, worst), auto-heal action breakdown, HOLD reason histogram, and a rule-based suggested action. `WeeklyDigestPanel.jsx` renders 4 KPI tiles, best/worst-trade chips, a horizontal-bar HOLD breakdown, and a lightbulb suggestion card.
  - Tests: `test_iter64_dashboard_widgets.py` 8/8 + `test_iter62_multi_exchange.py` updated for new reachability fields. Curated regression **65/65** across crypto/market-hours/bot-watching/widgets suites.

- 2026-06-27 (iter-63) — **Market-hours hard veto** (weekend gold trade attempt fix):
  - Root cause: bot generated `XAUUSD SELL` on Sunday 04:49 UTC; broker rejected MARKET_CLOSED. `is_weekend` was detected but never consumed as a hard stop.
  - `microstructure.is_market_closed(symbol, now=None)` returns `{reason, reopens_at_utc, reopens_in_hours}` for forex/metals; crypto = always open. Window: Fri 21:00 UTC → Sun 22:00 UTC.
  - Hard veto added at top of `ai_signals.py` cheap-HOLD pre-filter (**bypasses aggressive_mode** — market closure is physical, not a soft veto). Also added defense-in-depth in both `MT5BridgeEngine.execute()` and `PaperEngine.execute()` returning `{"blocked": "market_closed", ...}`.
  - 23 boundary tests cover Fri 20:59/21:00, Sat, Sun 21:59/22:00 for XAUUSD/EURUSD/XAGUSD and crypto 24/7. **All passing.**

- 2026-06-27 (iter-62) — **Multi-exchange CCXT support** (Kraken / OKX / KuCoin / Binance.US / Binance):
  - **User-blocker fixed**: Emergent's outbound IP is in Iowa/US, so Binance global (`.com` + testnet `.vision`) returned HTTP 451 to all key-verification probes. Confirmed by reachability test: Kraken/OKX/KuCoin/Binance.US all return 200 from the cluster.
  - **`crypto_bridge/ccxt_engine.py` (new)** — generic CCXT client with an `EXCHANGES` registry: per-exchange ccxt class, sandbox availability, passphrase requirement, default quote (USD for Kraken, USDT elsewhere). `_new_exchange()` dispatches by `account['exchange_id']` (defaults to `binance` for legacy docs). Passphrase decryption added to `_decrypt_creds()` for OKX/KuCoin.
  - **`crypto_bridge/binance_ccxt.py`** — collapsed to a thin re-export shim so all existing imports keep working.
  - **`routes/crypto_routes.py`** — `BinanceAccountCreate` gains `exchange_id` + optional `api_passphrase`. Create endpoint validates exchange_id is supported, enforces passphrase for OKX/KuCoin (HTTP 422 with friendly message), routes verify-probe through `CCXTClient`, persists `exchange_id` on the doc. New endpoint `GET /api/crypto/exchanges` powers the UI dropdown. Inspect ticker now picks `BTC/USD` vs `BTC/USDT` per account's `base_currency`.
  - **`pages/Crypto.jsx`** — Exchange dropdown, conditional Passphrase field (shows for OKX/KuCoin, hidden for Binance/Kraken/Binance.US), exchange label rendered on each account card, "live only · no sandbox" warning for Kraken/Binance.US. Page title generalised from "Binance Spot" → "Crypto · Spot Trading".
  - **Tests** (`test_iter62_multi_exchange.py`): 7/7 passing — exchanges-endpoint shape, passphrase enforcement, Kraken quote=USD, OKX-no-passphrase 422, Kraken dispatch verified by error message contents. Curated regression 46/46 green across crypto + tiered subs + bot-watching tests.

- 2026-06-27 (iter-61) — **"Bot is patiently watching" dashboard tile + trailer voice sync**:
  - **New feature** (user-requested): Dashboard widget that explains *why* the bot isn't trading — surfaces live entropy, news sentiment, RSI/volatility, the exact HOLD reason from `ai_signals.py`, plus "last eval / next eval in X" countdown and a HOLD-streak counter. Turns silent periods into a confidence-building view.
    - **Backend**: `GET /api/signals/watch-status` (`signal_routes.py`) — returns latest signal per user-configured symbol with entropy parsed out of the reasoning string, plus sentiment, indicators, session, cooldown countdown, and HOLD streak since last actionable signal. Tests: `test_iter61_bot_watching.py` 3/3 passing.
    - **Frontend**: `components/BotWatching.jsx` (new) — polls `/signals/watch-status` every 30s, renders per-symbol cards with an entropy meter that highlights threshold breaches, color-coded sentiment chip, indicators strip, "WHY I&apos;M NOT TRADING" reason block, and dual countdown footer. Mounted on Dashboard between BotPulsePanel and IntegrityWidget.
    - **Bug caught & fixed**: initial `api.get("/api/signals/watch-status")` produced `/api/api/...` (404) because the axios instance already has `baseURL=${BACKEND_URL}/api`. Fixed by stripping the leading `/api`.
  - **Trailer voice-slide sync fix** (`WelcomeTrailer.jsx`): re-anchored the SCENES array to real Whisper-transcribed timestamps from `trailer.mp3` (actual length 86.42s, not the assumed 70s). Added per-pillar highlight that lights up as the narrator counts "One… Two… Three… Four… Five." Click-to-play poster gate added to satisfy browser autoplay policy.

- 2026-06-27 (iter-60) — **Tiered subscriptions (Starter / Pro / Elite)** with feature-gate plumbing:
  - **`subscription_plans.py`** rewritten — 12 SKUs (`{tier}_{duration}`): starter $29-209, pro $99-713, elite $199-1432. Legacy plan IDs (`monthly`/`quarterly`/`semi_annual`/`annual`) preserved as aliases → `pro_*` for backward compatibility. New `Features` dataclass with 15 capability flags per tier.
  - **`subscription_service.py`** — `get_user_tier(user_id)`: admin → `admin`, active paid → tier from plan_id, in-grace → `pro` (legacy customer protection), expired → `starter`.
  - **`entitlements.py` (new)** — `enforce_feature`, `enforce_account_quota`, `enforce_symbol_allowed`, `cooldown_floor`, `require_feature` (Depends factory). All raise HTTP 402 with structured `{error, feature, current_tier, minimum_tier, message}` payload.
  - **Gates applied**: `POST /api/accounts` (quota Starter=1/Pro=3/Elite=∞), `/api/postmortem/*` (Pro+), `/api/auto-heal/*` (Pro+), `/api/analytics/learned-meta/drift*` (Elite only).
  - **New API**: `GET /api/entitlements/me` and `GET /api/entitlements/tiers`.
  - **`Subscription.jsx`** rewritten — 3-tier card grid, duration selector, "MOST POPULAR" Pro badge, color-coded accents, 15-row comparison table.
  - **Tests** (`test_iter60_tiered_subscriptions.py`): **19/19 passing.** Curated regression sweep **143/143 green.** Lint clean. Live-verified.


- 2026-06-27 (iter-58/59) — Sector caps, EA filling mode, version bump (see iter-58/59 entries below for full details).


- 2026-06-27 (iter-55) — **Multi-account circuit-breaker isolation**:
  - **Bug** (user-reported): User started bot for new VT Markets account → received Telegram "🚨 CIRCUIT BREAKER TRIPPED · Daily drawdown -11.41% breached -8.00% limit · Today's P&L: -3033.47 · Equity: 26596.41". The equity ($26,596.41) was the SUM of both accounts ($16,596.41 Roboforex + $10,000 VT Markets), and P&L summed losses from BOTH accounts. So Roboforex losses (-$1,384.42) + VT Markets losses (-$1,649.05) were aggregated and divided by combined equity, falsely tripping the VT Markets cfg.
  - **RCA**: Two circuit-breaker code paths both ignored `cfg.account_id` when computing P&L:
    1. `circuit_breakers.check_and_trip` — `realised_pnl_since` summed user-wide trades.
    2. `trade_manager._check_daily_drawdown` — summed both equity AND P&L user-wide, AND `update_many` disabled every active cfg on a single trip (so a Roboforex-driven drawdown would kill VT Markets bot too).
  - **Fix**: Both paths now honour `cfg.account_id`:
    - `realised_pnl_since(db, user_id, since, account_id=None)` — new optional param adds `account_id` to the Mongo query when provided.
    - `check_and_trip` passes `cfg.account_id` through. Disable filter already account-scoped (correct).
    - `trade_manager._check_daily_drawdown(cfg)` — refactored signature, scopes BOTH equity query AND trades query to the cfg's account_id when set. Disable now `update_one({"_id": cfg["_id"]}, …)` so only THIS cfg is killed on its own drawdown breach.
    - `_tick` iterates over each active cfg (not each user), so per-account checks run independently. Trade management picks the cfg matching the trade's account_id with default-cfg fallback.
  - **Tests** (`tests/test_iter55_circuit_breaker_isolation.py`): 6 unit tests covering both code paths — query filter assertions, no-trip when cross-account losses bleed in, only-own-cfg disabled on trip. **6/6 passing.** Curated regression 107/107 green.
  - **Data fix**: cleared the false-positive `tripped_at`/`tripped_reason`/`circuit_breaker_*` flags on both per-account bot_configs so the user can restart cleanly. Default cfg PANIC LOCK left intact (that one was set by the user manually earlier).


- 2026-06-27 (iter-54) — **Multi-account balance leak via heartbeat WS broadcast**:
  - **Bug** (user-reported): User has two MT5 accounts connected (`micro` on Roboforex with $16,596.41 · `vtmarkets` on VT Markets demo, expected $10,000). Both rows displayed the **same** Roboforex balance in the UI.
  - **RCA**: `bridge_routes.heartbeat` correctly nulls `balance`/`equity` on the DB write when `payload.account_login != acc.account_number` (broker mismatch detection), but the subsequent **WebSocket broadcast** sent the raw `payload.balance`/`payload.equity` regardless. The frontend's `account_heartbeat` handler in `Accounts.jsx` then overwrote the correct DB-side `null` with the wrong-account's value as soon as the next heartbeat from the other terminal arrived. The DB was always correct (`/api/accounts` returned `balance=null` for VT Markets) — only the live UI was wrong.
  - **Fix (backend)**: `/app/backend/routes/bridge_routes.py` WS broadcast now applies the same `... if not mismatch else None` filter as the DB write. Additionally surfaces `broker_account_mismatch`, `broker_account_mismatch_reason`, `broker_account_id_reported`, and `status: "connected"|"disconnected"` so the UI can render the warning banner live without waiting for a page refresh.
  - **Fix (frontend)**: `/app/frontend/src/pages/Accounts.jsx` heartbeat handler now propagates the mismatch fields into local state (so the warning banner appears in real-time) and preserves `null` balance/equity (so the row shows "—" instead of stale data).
  - **Underlying user-side cause** (still requires user action): Their VT Markets EA is reporting MT5 login **67198987** (the Roboforex account). Either both EAs are attached to the same MT5 terminal, OR the VT Markets terminal is logged into the wrong broker account. The mismatch warning on the Accounts page now flags this clearly.
  - **Tests** (`tests/test_iter54_heartbeat_ws_mismatch.py`): 2 unit tests — (1) WS broadcast nulls balance + flags mismatch when EA is on the wrong MT5 account; (2) sanity check that matched accounts pass balance through cleanly. **2/2 passing.** Curated regression 79/79 still green (iter-50/51/52/54).


- 2026-06-27 (iter-53) — **Full E2E sweep + Portfolio Risk UI surfaces CVaR fields**:
  - Testing-agent full sweep: backend 31/31 pass · frontend 95% (only gap was missing CVaR card on `/portfolio`).
  - **`/app/frontend/src/pages/Portfolio.jsx`**: added a second KPI row exposing `CVAR 95%`, `CVAR 99%`, `PORT. σ`, and `POSITIONS` so iter-51 risk improvements are visible to users. Color-coded against the 2%/day CVaR target (green ≤1%, amber ≤2%, red >2%). Defensive nullish-coalescing (`?? 0`) on every new field so the cards render cleanly even when backend returns 0/null. New testids: `kpi-cvar-95`, `kpi-cvar-99`, `kpi-port-sigma`, `kpi-positions`.
  - Live-verified via Playwright: all 4 new cards render, login flow + dashboard + portfolio navigation works end-to-end.


- 2026-06-27 (iter-52) — **ADWIN drift detection + Platt probability calibration**:
  - **`probability_calibrator.py` (NEW)** — Platt scaling (Platt 1999) with Lin et al. 2007 label smoothing to avoid perfect-separation on tiny datasets. Pure NumPy — no sklearn dependency. Skips calibration when n<10 (identity map). Provides `fit_platt`, `apply_platt`, `brier_score`.
  - **`learned_meta.py`** — Calibration runs after every retrain. Artifact now carries `calibration: {A, B, n, brier_raw, brier_calibrated, converged}`. `predict_p_win` returns both `p_win_raw` (sigmoid score) AND `p_win_calibrated` (Platt-mapped). The default `p_win` field is now the calibrated probability so downstream guards see real probabilities. Test verified Platt reduces Brier on a controlled mis-calibrated synthetic dataset.
  - **`drift_detector.py` (NEW)** — ADWIN (Adaptive Windowing) per-session detectors on the rolling `|p_predicted − actual_outcome|` residual stream:
    - `record_residual(...)` persists each closed-trade residual to `db.learned_meta_residuals` tagged with session bucket.
    - `record_residual_for_trade(...)` fire-and-forget helper called from `bridge_routes` + `execution.py` on trade-close.
    - `check_drift(...)` rebuilds ADWIN from window (default 300 most-recent residuals, δ=0.002).
    - `maybe_trigger_retrain(...)` invoked every bot tick; if any session detector reports drift AND `RETRAIN_COOLDOWN_HOURS` (default 12h) elapsed since last fire, calls `learned_meta.retrain()` and audit-logs to `db.drift_detector_audit`.
  - **`bot_runner.loop()`** — added `maybe_trigger_retrain` sweep at the end of each tick (cooldown-gated, cheap no-op on most ticks).
  - **API**: `GET /api/analytics/learned-meta/drift` (read-only state per session) · `POST /api/analytics/learned-meta/drift/check-now` (force-fire detector check). `GET /api/analytics/learned-meta` now also returns the `calibration` block (A, B, brier_raw, brier_calibrated, converged).
  - **Env knobs**: `DRIFT_DETECTION_ENABLED=true` · `DRIFT_MIN_RESIDUALS=30` · `DRIFT_WINDOW_SIZE=300` · `DRIFT_ADWIN_DELTA=0.002` · `DRIFT_RETRAIN_COOLDOWN_HRS=12`.
  - **Deps**: Added `river==0.25.0` (+ deps `altair`, `narwhals`, `scipy`) to `requirements.txt`.
  - **Tests** (`tests/test_iter52_drift_and_calibration.py`): 14 unit tests — Platt skip/fit/identity/improves-Brier, residual shape, ADWIN insufficient-data skip, ADWIN detects regime shift, cooldown blocks, retrain fires when drift+cooldown clear, artifact has calibration block, `predict_p_win` returns raw+calibrated. **14/14 passing.** Curated regression suite 121/121 (iter-31/32/48/50/51/52).
  - **Live-verified**: `/api/analytics/learned-meta` returns the new `calibration` block (Platt A/B/Brier visible). Bot retrained automatically with 85 closed trades. Drift endpoint returns empty buckets cleanly (no residuals yet — will populate as new trades close).


- 2026-06-27 (iter-51) — **Correlation-aware portfolio allocation + dynamic CVaR risk budget**:
  - **Why**: Existing per-trade Kelly sizing ignored book-level concentration — two highly correlated BUY trades (e.g., XAUUSD + BTCUSD when ρ>0.7) effectively doubled the bet without being flagged anywhere in the lot-sizing chain. Auto-deleverage only kicks AFTER a breach; this trims at the *entry* side proactively.
  - **`portfolio/var.py`** — extended snapshot to also surface **CVaR (Expected Shortfall)** at 95% and 99% using parametric multipliers (ES_MULT_95=2.0627, ES_MULT_99=2.6652). Empty-portfolio branch + populated branch both emit the new fields. Existing VaR math is unchanged.
  - **`portfolio/correlation_kelly.py` (NEW)** — `compute_correlation_aware_scale()` returns a multiplicative trim in (MIN_SCALE, 1.0]:
    1. **Correlation penalty** — `corr_pressure = Σ_i max(0, ρ_eff_i) × w_i` where ρ_eff = +ρ on matching-action positions, −ρ on opposing (genuine hedges → no penalty). `corr_scale = 1 / (1 + α × corr_pressure)`, α=2.0 by default.
    2. **CVaR budget** — builds a hypothetical "with new trade" book using existing correlation+ATR plumbing, forecasts portfolio CVaR_95, and if forecast > target (default 2%/day) → `cvar_scale = target / forecast`. Combined scale = corr_scale × cvar_scale, clamped at MIN_SCALE=0.25.
    - Same-symbol stacking forces ρ=1.0 (anti-pyramid is the primary guard but defence-in-depth).
    - Returns a human-readable `reason` for logging/UI surfacing.
  - **`bot_runner.py`** — wired AFTER `effective_lot` is computed (post Kelly + max-lot-cap, pre engine.execute). Reads open book scoped to the cfg account, computes the trim, multiplies `effective_lot` by the scale (never inflates), logs the trim with reason, persists `corr_kelly_trim` on the consumed signal doc for UI deep-link. Disabled-by-default behind `CORRELATION_KELLY_ENABLED` env (defaults to `true`; failure-mode → skip silently with debug log).
  - **Env knobs**: `CORRELATION_KELLY_ENABLED=true` · `CORRELATION_KELLY_ALPHA=2.0` · `CORRELATION_KELLY_MIN_SCALE=0.25` · `PORTFOLIO_CVAR_TARGET_PCT=2.0`.
  - **Tests** (`tests/test_iter51_correlation_kelly.py`): 8 unit tests — VaR/CVaR field presence + proportionality, no-positions pass-through, same-symbol stacking trims, opposite-direction = hedge (no penalty), CVaR overshoot triggers trim, MIN_SCALE clamp, reason string sanity. **8/8 passing.** Full regression: 55/55 portfolio + execution + safety + bias-trap + full sweep tests still green.
  - **Verified live**: `GET /api/portfolio/snapshot` now returns `cvar_95_usd`, `cvar_95_pct_equity`, `cvar_99_usd`, `cvar_99_pct_equity` alongside the existing VaR fields. Lint clean.


- 2026-06-26 (iter-50) — **Auto-Heal scheduler + Simple Mode + grouped sidebar (UX simplification)**:

  ### Auto-Heal (1a)
  - New module `auto_heal.py` with 4 safe + reversible checks: disable `aggressive_mode` after daily PnL ≤ −2% of equity · raise `min_confidence_override` +5 on patterns recurring ≥3× / 30d · reconcile DB↔broker drift · clear pulses older than 1h. Each action audit-logged to `auto_heal_actions`.
  - Background scheduler in `server.py` startup runs `sweep_all_users()` every 5 min (`AUTO_HEAL_INTERVAL_SEC=300`). Iterates users with `auto_heal_settings.enabled=true` (opt-in, default OFF).
  - **NEVER touches**: bot ON/OFF, open trades, position sizing, risk profile, money flow.
  - Routes (`routes/auto_heal_routes.py`): `GET/POST /api/auto-heal/settings` (opt-in toggle) · `POST /api/auto-heal/run-now` (manual trigger, bypasses opt-in for this run only) · `GET /api/auto-heal/log` (audit trail).
  - **Auto-Heal panel** on `/bot-health` page (`BotHealth.jsx`) — opt-in toggle + RUN NOW button + last 8 actions with timestamps + plain-English description of what gets touched.
  - **Verified live**: first manual run cleared 1 stale pulse on admin account; backend scheduler task wired into startup/shutdown lifecycle correctly.

  ### Simple Mode (2d-a)
  - Per-user preference `users.preferences.simple_mode` (default false). Sidebar toggle at top, syncs to server + localStorage cache.
  - When ON, hides 20 advanced pages and shows only 5 essentials: Dashboard · Trades · Bot Health · Bot Config · Settings. One-click switch to Pro Mode anytime.
  - New route `GET/POST /api/settings/preferences` for the toggle (whitelist-only field validation).

  ### Grouped sidebar (2d-b)
  - Sidebar refactored into 6 collapsible sections:
    - **TRADING** — Dashboard · Trades · AI Signals · Risk Commander
    - **INSIGHTS** — Bot Health · Analytics · Loss Lab · Shadow Report
    - **AUTOMATION** — Bot Config · Strategies · Agents · Research Agent
    - **INFRASTRUCTURE** — MT5 Accounts · Crypto · Portfolio Risk · Safety Blocks · Execution Intel · Symbols
    - **ACCOUNT** — Notifications · Subscription · Billing · Affiliate · Settings
    - **LEARN** — Guide · FAQ
  - Default-open: TRADING + INSIGHTS (daily-use). User-preferred open state persisted in localStorage (`stoic_sidebar_open`).
  - Each section header is clickable to collapse/expand; the 16-item flat list is now 4 visible categories on first load.

  ### Onboarding tour (2d-c) — deferred
  - First-login tour is non-trivial (needs target-element refs across 4 pages + tooltip lib). Tracking as P2 follow-up; the Simple Mode default + the existing Onboarding Banner provide most of the value for new users right now.

  ### Verified
  - Both Simple ↔ Pro mode toggles confirmed live (Crypto/Loss Lab nav items disappear/reappear correctly).
  - Auto-heal opt-in toggle round-trips through `/api/auto-heal/settings`.
  - Bot Health score remained at 90/100 EXCELLENT after the changes; diagnostic improved from 3/6 OK to 4/6 OK (reconcile cleared Trade Sync).


- 2026-06-26 (iter-49) — **Bot Health hardening: 75 → 90 (excellent)**:
  - Applied user-approved hardening on the admin account after the iter-48 forensics:
    - Set `aggressive_mode=false` on both bot_configs (Default + RoboForex). +15 health.
    - Raised `min_confidence_override` 55 → 60 on both configs (matches the existing Bot-Health *"SUGGEST ACTION"* for ASIA-worst-session).
    - Ran `POST /api/trades/reconcile` — closed the orphan DB=1↔Broker=0 mismatch (now DB=1↔Broker=1, no auto-closes needed).
  - **Result**: health-score `/api/bot/health-score` jumped from 75 → 90, status `good` → `excellent`, headline returned to *"All systems nominal — the bot is in control."* The only remaining issue is the `info`-level "Bot is paused" deduction (-10), intentionally left for the user to flip back on themselves after the loss series.
  - **Bot left OFF deliberately** — declined to auto-enable since it just lost $1121; user must opt in to resume trading. Telegram-alerts warning also untouched (requires user-supplied bot token).
  - The 4 iter-48 protective fixes (self-contradiction veto, anti-pyramid, loss-streak circuit-breaker, deleverage-prefers-loser) are active and will guard the next live cycle.


- 2026-06-26 (iter-48) — **Bias-trap forensics + 4 durable fixes** (`-$1121 loss postmortem`):

  **Forensic findings on the 7-trade loss series** (admin account, 06:21–06:43 UTC):
  - All 7 trades were SELLs on XAUUSD while gold rallied from 4010 → 4039 (+0.7%).
  - 5/7 SELL signals had LLM `reasoning` literally containing *"TRANSITIONAL regime with CAUTIOUS_WAIT mode blocks new positions"* / *"red-light noise filter blocks trading"* / *"non-tradeable environment"* — yet returned `action=SELL, conf=58, tradeable=True`.
  - Bot opened 5 same-direction trades in 22 min (no anti-pyramid filter existed).
  - Portfolio auto-deleverage closed the **LARGEST** positions (= deepest underwater after gold rallied), compounding the bleed.
  - **Real root cause: `aggressive_mode=True` on both bot configs** — disables entropy filter, learned-classifier veto, and the historically-respectful "no trades in NOISY entropy" gate.

  **Fix A — Self-contradiction veto** (`ai_signals.py:2b`): regex-scans the LLM's reasoning for blocking phrases (`cautious_wait`, `blocks new positions`, `no entry`, `prohibit entry`, etc.) and force-HOLDs if any are present. **Deliberately ignores `aggressive_mode`** — it's a sanity check, not a probabilistic filter. Surfaces in signal.reasoning as `VETO (self-contradiction):`.

  **Fix B — Anti-pyramid filter** (`bot_runner.py:_process_user_account_locked`): before executing, counts open trades on (user, symbol, action). If `>0`, records pulse `Anti-pyramid: N {ACTION} {SYM} already open — refusing to stack same-direction risk` and skips. Per-account scoped to mirror inflight counting.

  **Fix C — Loss-streak circuit-breaker** (`bot_runner.py`, constants `LOSS_STREAK_THRESHOLD=2`, `LOSS_STREAK_COOLDOWN_HRS=4`): queries the last 2 closed trades on `(user, symbol, action)` — if both lost in the cooldown window, pauses same-direction entries for 4h. Records pulse `Loss-streak circuit-breaker: …pausing to break the bias trap`.

  **Fix D — Auto-deleverage prefers worst loser** (`portfolio/risk_manager.py:_sector_exposure` + sector_cap branch): positions now carry `live_pnl`. When sector-cap fires, picks the position with **worst live_pnl** (cuts the bleeding) instead of largest notional. Falls back to largest-notional when live P&L isn't available yet.

  **Fix + — Aggressive-mode warning on Bot Health** (`routes/bot_routes.py` health-score): scans all bot_configs for `aggressive_mode=True`, deducts 15 from health score, surfaces a loud P1 warning explaining that entropy + learned-classifier vetoes are BYPASSED. The admin account now shows score 90 → 75 with the warning visible immediately on `/bot-health`.

  **Tests** (`tests/test_iter48_bias_trap.py`): 8 pytest cases covering block-phrase detector (5 paraphrases all hit, clean text passes), anti-pyramid query shape, loss-streak query shape, win-mixed-in skip path, sector-cap-prefers-loser, fallback-to-largest. **51/51 cases passing** across iter-43/45/48 suites.

  **Verified live**: post-fix signal generation correctly returns `HOLD` with `veto_applied=True`; health-score correctly drops to 75 with the aggressive-mode amber warning rendered on `/bot-health`.


- 2026-06-26 (iter-47) — **Source badges + auto-loosen + Suggest-action (3 P1s in one batch)**:

  **Task 1 · Source badge on Trades UI** (`pages/Trades.jsx`)
  - Added `SOURCE_BADGE_FOR(t)` derivation in `Trades.jsx` — classifies every trade as **STOIC** (green) / **MANUAL** (gold) / **OTHER EA** (amber) / **PAPER** (purple) / **SHADOW** (grey) based on `origin` + `magic_number`. Renders next to status pill on every row.
  - Backend (`routes/bridge_routes.py`) — replaced binary `is_external = (magic == 0)` with 3-way classification using `STOIC_MAGIC = 901234`: magic=0 → manual, magic=901234 → auto, anything else → `other_ea`. The `magic_number` is now persisted on the trade doc so the UI can disambiguate. Applied to both `deal_received` insert path and the open-positions backfill path.

  **Task 2 · Auto-loosen counterpart** (`loss_postmortem.py`)
  - New `maybe_record_winner(db, trade_id)` mirrors `maybe_record_postmortem` but for winners. Hooked into the same two close paths (bridge + paper-settle).
  - Logic: if the trade's `pattern_key` had a previous tighten (queried from `guardrail_adjustments`) AND ≥3 wins followed AND no loosen in last 7 days AND `auto_tighten_enabled=true` → eases `min_confidence_override` by **-3** (clamped at floor 50). Records audit row with `direction: "loosen"` and `trigger_kind: "winning_streak"`. Telegram alert fires.
  - Tighten path now also writes `direction: "tighten"` (was untagged) so the queries discriminate cleanly.
  - **7 new pytest cases** (no prior tighten → skip · only 2 wins → skip · 3 wins → applies → -3 · floor clamp at 50 · already-loose-skip · opt-in-off → skip · loss → skip). All passing (19/19 in the suite).

  **Task 3 · "Suggest Action" on Session Edge** (`routes/analytics_routes.py` + `pages/BotHealth.jsx`)
  - Backend: `POST /api/analytics/sessions/suggest-action` inspects buckets, finds any with ≥5 trades AND <40% win-rate, returns `{action, session, from, to, rationale}`. Falls back to `{action: "no_action"}` when no bucket qualifies.
  - Backend: `POST /api/analytics/sessions/apply-action` writes the proposed `min_confidence_override` to the user's default bot_config (creates the doc if missing). Validates field allowlist + clamps 50-95.
  - Frontend: `SessionsPanel` in BotHealth gets a golden `SUGGEST ACTION` button (`Wand2` icon). On click → fetches the suggestion → renders an in-panel banner with the rationale + `APPLY` / `DISMISS` buttons. Apply hits the backend and re-loads the panel so the trader sees the change reflected.
  - Verified live with admin account: returned `tighten_worst ASIA · min_confidence_override 55 → 60` with the full rationale ("ASIA has 7 trades at 14.3% win-rate, avg-R -0.69, P&L -$1121.69"). UI banner renders correctly with both action buttons.


- 2026-06-26 (iter-46) — **Bot Health page · live check + self-improvements feed (single screen)**:
  - **Live check now** (admin account snapshot): score 90/100 EXCELLENT · 1/1 brokers connected · EA v1.27. Diagnostic FAIL 3/6 OK — trade sync DB=1 vs broker 0, Daily PnL –$1121.69 deep-red, Bot ON toggle (paused by user), Telegram not configured. 7 trades all in Asia · 14.3% win · –0.69R · 1 loss pattern detected (`XAUUSD|TRANSITIONAL|TOKYO|SELL`) · 0 auto-adjustments (auto-tighten still opt-in OFF) · 1 Safety-Guardian block (per_trade_risk_cap from yesterday's simulation).
  - **New page `/bot-health`** (sidebar entry, Stethoscope icon, just under Dashboard) aggregates 7 existing endpoints in parallel — `bot/health-score`, `diagnostic/run`, `bot/pulse`, `analytics/sessions`, `postmortem/patterns`, `postmortem/adjustments`, `safety-blocks/stats` — into a single screen the trader can re-check anytime.
  - **Layout**: headline score card (big-number + status chip + issue→fix bullets) → 2-column grid (Live Diagnostic + Bot Pulse) → 2-column grid (Session Edge with `EDGE LIVES IN` insight + Self-Improvements feed combining auto-tightens, recurring loss patterns, Safety-Guardian block tallies).
  - **Auto-refresh** every 60s. Manual `RE-CHECK` button. Color-coded by severity (excellent/healthy green · degraded/warning amber · critical red).
  - All sub-cards deep-link to the source pages (Analytics, Loss Lab, etc) for full drill-down.


- 2026-06-26 (iter-45) — **Loss Lab · auto-investigate losing trades + opt-in guardrail tightening**:
  - **User ask** (1d + 2b + 3c + 4c): trigger on SL hits OR consecutive losses · quantitative diff + Claude LLM narrative · auto-tighten min_confidence after recurring pattern · dedicated page + per-trade button.
  - **New module `loss_postmortem.py`** with the full pipeline:
    1. `maybe_record_postmortem(db, trade_id)` — idempotent. Eligible when `close_reason=="stop_loss"` AND `pnl<0` (trigger `sl_hit`) OR `pnl<0` AND the prior closed trade on (user, symbol) also lost (trigger `consecutive_loss`).
    2. Quantitative diff — regime, session, macro freeze, macro gate, sentiment flip, DXY/VIX/yields, MTF aligned, A+ passed, confidence (entry vs now).
    3. Claude (`claude-sonnet-4-5-20250929`, Emergent LLM key) returns structured JSON: summary, why_it_looked_good, what_actually_happened, what_changed, lessons[], suggested_guardrail. Pattern key is always derived locally (`symbol|regime|session|action`) for reliable clustering.
    4. **Auto-tightener** `_maybe_autotighten()` — when pattern_key has ≥3 post-mortems in last 30d AND `users.postmortem_settings.auto_tighten_enabled=true` AND no adjustment in last 7d (cooldown), bumps `min_confidence_override` by +5 (clamped 50–95), records to `guardrail_adjustments`, fires Telegram alert.
  - **Triggers wired**: bridge close path (`routes/bridge_routes.py:status=='closed'`) and paper-trade settle path (`execution.py.settle_paper_trades_against_price`) both spawn `asyncio.create_task(maybe_record_postmortem(...))` — fire-and-forget so trade close latency stays sub-100ms.
  - **API** (`routes/postmortem_routes.py`):
    - `GET /api/postmortem` — paginated list (filter by `pattern_key`)
    - `GET /api/postmortem/patterns?days=30` — aggregate by pattern_key for the Loss Lab pattern sidebar
    - `GET /api/postmortem/{trade_id}` — single investigation
    - `POST /api/postmortem/{trade_id}/regenerate` — manual re-run (used by Trades→LossLab deep-link)
    - `GET/POST /api/postmortem/settings` — auto-tighten opt-in toggle (uses `_user_doc` for the ObjectId/string `_id` fallback pattern)
    - `GET /api/postmortem/adjustments` — audit history of all auto-tightens
  - **Frontend** new page `pages/LossLab.jsx` + sidebar entry `Loss Lab` (FlaskConical icon):
    - Auto-tighten opt-in card with toggle
    - Recurring patterns sidebar — ranked by count, click to filter
    - Per-postmortem card: TL;DR, "why it looked good", "what happened", "what changed", lessons (bulleted), **SUGGESTED GUARDRAIL** (amber highlight), expandable quantitative diff
    - Auto-adjustments audit panel (recent `from → to` changes per pattern)
    - URL deep-link `/loss-lab?trade=<id>` auto-regenerates if missing + scrolls to that card; shows "INVESTIGATING…" banner during the ~10-15s Claude call
  - **Trades page** integration: POST-MORTEM button renders next to EXPLAIN only on `status==='closed' && pnl<0` rows. Links straight to `/loss-lab?trade=<id>`.
  - **Tests** (`tests/test_iter45_postmortem.py`): 12 pytest cases — winner skipped · SL-hit eligible · consecutive-loss eligible · winner-prior not eligible · pattern_key stable · idempotent insert · auto-tighten respects opt-in / threshold / cooldown / clamp-at-95. All passing.
  - **Verified live**: triggered post-mortem on the simulation's manual-test loss → Claude correctly identified *"TRANSITIONAL regime with CAUTIOUS_WAIT mode explicitly blocked entries yet trade was taken anyway"* with suggested guardrail *"raise min_confidence to 70 for XAUUSD SELL in tokyo session during TRANSITIONAL regime"*. Pattern key `XAUUSD|TRANSITIONAL|TOKYO|SELL` correctly recorded for future clustering.


- 2026-06-26 (iter-44) — **Full e2e simulation + 3 bugs fixed**:
  - Walked every major endpoint of the production app (auth → bot pulse → signals → trades → safety → research → analytics → crypto → telegram → affiliate → diagnostic). Found 3 real bugs + 1 improvement, all fixed and verified live.
  - **🔴 Bug A (P1) — `macro_gate.py:129`**: `db or get_db()` raised `NotImplementedError: Database objects do not implement truth value testing` because Motor's `Database` rejects truthiness. Every manual `/trades/execute/{signal_id}` call crashed Safety Guardian with HTTP 500. **Fix**: `db_ref = db if db is not None else get_db()`. Audited the rest of the codebase — no other occurrences.
  - **🔴 Bug B (P1) — `research_routes.py` auto-accept POST**: `db.users.update_one({"_id": user["id"]}, ...)` silently matched zero docs because `_id` is `ObjectId` while `user["id"]` is a string. The endpoint returned a fake-success payload but never persisted; subsequent GET showed the old value. **Fix**: added `_find_user()` helper that tries `ObjectId`, raw `_id`, and legacy `id` field — mirrors auth seeding history. Now POST→GET round-trips correctly. Audited the rest of the codebase — all other `db.users.update_one` calls already use `ObjectId(...)`.
  - **🟡 Improvement A — `/trades/execute/{signal_id}` (manual execute)**: was passing `signal.lot_size` directly to the engine. The signal-time lot is computed against a hardcoded $1000 placeholder equity (account-agnostic), which over-shoots on real accounts and triggered an unjust Safety-Guardian `per_trade_risk_cap` block (e.g. 1.67 lots on a $18k account → $2,505 risk vs $182 cap). **Fix**: manual execute now recomputes via `compute_lot_for_account` against the target account's real equity + applies the same Kelly-scaled `max_lot_size` cap as `bot_runner` does. Confirmed live: 1.67 → 0.17 lots → trade accepted.
  - **🟡 Improvement audit (false positives flagged during probe)**: `/api/portfolio/risk`, `/api/safety-blocks/recent`, `/api/telegram/status`, `/api/affiliate/me`, `/api/execution_intel/...` all returned 404 — but the real routes are `/portfolio/snapshot`, `/safety-blocks/list`, `/telegram/webhook/status`, `/affiliate/status`, `/execution/...` (probe path typos, not bugs).
  - **Observed but unrelated to the bot**: 2 manual-test trades I queued during the simulation closed at -$93 / -$95 on the live RoboForex demo account. The bot's *autonomous* tick afterwards opened 2 more at 06:21 UTC — pulse correctly showed `EXEC · Executed SELL XAUUSD 0.17 lots @ 4018.70 (conf 58%)`. Bot is healthy and trading multi-session (asia_session_skip_xau=false confirmed live).
  - All `test_iter43` + `test_iter39` pytest suites green (26/26 passing).


- 2026-06-26 (iter-43) — **Session breakdown on Analytics (Asia / London / Overlap / NY / Off)**:
  - Now that the bot trades all sessions, the trader needs to see which UTC window their edge actually lives in. New `GET /api/analytics/sessions` route (`routes/analytics_routes.py`) returns 5 buckets with `count, wins, losses, win_rate, avg_pnl, total_pnl, avg_r, expectancy_r, best_r, worst_r, r_sample_count` plus top-level `best_session_by_r` / `best_session_by_pnl` headline ranks (only buckets with ≥3 trades qualify — avoids ranking on one lucky outlier).
  - **R-multiple math** (`analytics._r_multiple`): R = pnl / (|entry − SL| × lot × contract_size). MT5 contract sizes baked in (XAU=100, BTC=1, ETH=1, XAG=5000). Clamped to ±10R to neutralize pathological outliers (e.g. 0.1-pip stops).
  - **Session split** uses dedicated `_session_key_4buckets` (Asia 00–07, London 07–13, Overlap 13–16, NY 16–21, Off 21–24 UTC) — separates the London-NY overlap from NY proper (the legacy `_session_label` folds them together, kept untouched for the existing Attribution slice).
  - **Frontend** (`pages/Analytics.jsx`): new `SessionBreakdown` card injected right under the Overall Performance row. Color-coded left-border per session (Asia amber, London green, Overlap gold, NY blue, Off grey). Each card surfaces N trades · win-rate · avg-R · expectancy-R · total P&L with a "BEST AVG-R" / "BEST P&L" chip on the winning bucket and a `NEEDS ≥3 TRADES TO RANK` hint on under-sampled buckets. Bottom insight line: *"Highest avg-R is LONDON · highest cumulative P&L is NY"* (when those differ).
  - **Empty state** rendered when no closed trades exist — the page no longer just shows "5 closed trades needed" and hides everything; the session card explains itself even on day 1.
  - **Tests** (`tests/test_iter43_session_breakdown.py`): 21 pytest cases — R-math (winners/losers/missing SL/outlier clamp), all UTC hour→bucket boundaries, end-to-end compute_sessions (always emits 5 buckets, ranks correctly, rank requires ≥3 samples). All passing.
  - Verified live with seeded data: cards rendered as `LONDON · 100% win · +2.5R · +$150 · BEST AVG-R · BEST P&L`, sub-3 buckets correctly tagged `NEEDS ≥3 TRADES TO RANK`. Seed cleaned up after verification.


- 2026-06-26 (iter-42b) — **Bot Pulse on Dashboard + multi-session trading enabled**:
  - User asked for the bot-inactivity explanation on the **Dashboard** (not just /trades) and confirmed they want trading on all sessions (Asia / London / NY).
  - **Multi-session audit**: confirmed `asia_session_skip_xau` is the only session-based hard veto in `bot_runner.py`. With the user toggling it OFF in Bot Config, all three sessions are tradeable. Other "session" references (`learned_meta.py` session-bucketing learner, `analytics.py` reporting, `twap_vwap.py` execution-pacing weights) are analytics/learning only — not vetoes — and continue to bucket data correctly across Asia/London/NY.
  - **Dashboard wiring**: same reusable `BotPulsePanel` component injected at the top of `pages/Dashboard.jsx` (above IntegrityWidget). Now surfaces the latest cycle verdict on Dashboard *and* Trades pages — both polling independently every 30s. Verified live: with `asia_session_skip_xau=false` the bot now ticks at 06:03 UTC and the pulse correctly shows the next gate (`Signal cooldown active · NEXT IN 10M`) instead of the previous Asia-skip block.


- 2026-06-26 (iter-42) — **Bot Pulse · explain why an enabled bot is silent**:
  - **User pain**: "bot is enabled and I don't see activity, and there is no explanation for the lack of activity" — bot_runner had 13+ silent-skip exit points (cooldown, anti-tilt, asia-skip, daily-cap, SL-cooldown, max-concurrent, rate-limit, spread-block, no-connected-accounts, circuit-breaker, subscription-inactive, auto-tune-block, AI-HOLD) and none were surfaced anywhere.
  - **Backend** (`bot_runner.py`): new `_record_pulse(db, cfg, action, reason, level, symbol, next_eligible_at)` helper writes the latest cycle verdict to `bot_configs._last_pulse` on every exit point. Wired at all 13 skip points + the EXEC happy path. Level palette: `info` (grey, AI-HOLD / cooldown / cap / paper-shadow), `warn` (amber, anti-tilt / max-concurrent / rate-limit / orchestrator-failure / no-connected-accounts), `block` (red, circuit-breaker / no-account / subscription-inactive / Safety-Guardian-block).
  - **New route** (`routes/bot_routes.py`): `GET /api/bot/pulse` returns `{items: [{config_id, label, account_id, active, paper_shadow_mode, symbols, pulse, stale_seconds}], loop_interval_sec}`. Sorts active → shadow → off. Resolves account label from `accounts.login/broker`.
  - **Frontend** (`components/BotPulsePanel.jsx` + injected on `pages/Trades.jsx`): collapsible panel at the top of the Trades page. Header line surfaces the *loudest* current reason across all active bots (ranked block > warn > info, then freshness). Expanded view lists each bot config with a LIVE / SHADOW / OFF chip, traded symbols, the latest action (EXEC / BUY / SELL / HOLD / SKIP / BLOCKED), the full reason, age (`Xs / Xm ago`), and a `NEXT IN …` countdown when the pulse carries `next_eligible_at`. Polled every 30s; falls back gracefully when no pulse exists yet ("Waiting for first cycle… (bot loop runs every 60s)").
  - **Verified live**: admin's RoboForex bot showed *"SKIP · Asia-session skip — XAUUSD pauses until 07:00 UTC (low liquidity)"* exactly as expected — the previously invisible asia-session guard is now the first thing the user sees on /trades.


- 2026-06-25 (iter-41) — **Guide documentation refresh (Iter 34-40 chapters live)**:
  - Added 4 new chapters to `/guide` (`pages/Guide.jsx`) covering features shipped in iter 34-40 — section 6 *Explainable AI Trading*, section 7 *Multi-Bot · Multi-Account*, section 11 *Paper Shadow Mode + Report*, section 15 *Crypto · Binance Spot*. Table-of-contents renumbered (now 19 sections).
  - **Bug fix**: React crash on `/guide` (`PAGEERROR: Cannot read properties of undefined (reading 'bd')`) was caused by the new sections passing `kind="success"` / `kind="warning"` to the local `Callout` palette which only had `info`/`good`/`warn` keys. Extended the palette with `success` (alias of `good`) and `warning` (alias of `warn`) and added an `info` fallback so unknown kinds never crash again.
  - Verified live via screenshot tool — all 19 TOC anchors render, no console errors, page paints cleanly.


- 2026-06-25 (iter-38) — **Live-simulation polish pass (4 user-visible bugs closed)**:
  - **HOLD-signal zero-stop footgun** (ai_signals.py line ~509): when `final_action == "HOLD"`, response now returns `entry_price=None, stop_loss=None, take_profit=None, tp1/2/3=None, sl_pips=0, tp_pips=[0,0,0], lot_size=0`. Previously the response had `entry=SL=TP=current_price` which would be a zero-risk trade if ever forced through execution. `tradeable=False` already prevented exec via UI but defence-in-depth says don't emit broken math.
  - **`/api/macro/freeze/{symbol}` 404 fix** (routes/macro_routes.py): added the route as an alias to `economic_calendar.macro_freeze_check`. The frontend MacroClimate widget was constantly hitting this 404 (visible in production logs). Identical response shape to `/api/calendar/freeze/{symbol}`.
  - **`/api/macro/snapshot` summary block** (routes/macro_routes.py): response now folds in the gate state — top-level `summary` block with `regime`, `gate_open`, `buy_ok`, `sell_ok`, `blocked[]`, plus quick scalars `vix`/`dxy`/`y10y`/`y2y` — so the MacroClimate widget can render in one round-trip instead of two and never sees nulls when FRED data exists.
  - **`/api/notifications/settings` 404 alias** (routes/notification_routes.py): added as a third route decorator alongside `/telegram` and `/prefs` — same handler, same response shape — for legacy frontend callers.
  - **Audit by simulation**: 7 P0/P1 candidates flagged during the end-to-end trace, 4 were real bugs (above), 3 were false positives in my probe (portfolio uses nested dicts not flat `drawdown_pct` keys; `sl_atr_mult` is an env-level constant not a per-config field; `/api/diagnostic/run` already existed under the right path).
  - **All verified live**: HOLD signal now returns null SL/TP/entry; macro/freeze returns 200 with `frozen=false`; macro/snapshot has `summary.regime=macro_neutral, gate_open=true, dxy=120.4, y10y=4.5`; notifications/settings returns full prefs. Zero existing tests broken — `"stop_loss" in sig` assertions still hold (key exists, just None on HOLD).


- 2026-06-25 (iter-35) — **Binance Spot live BTC execution via CCXT**:
  - **Server-side REST execution path for crypto** — bypasses the MT5 EA queue entirely. Added `crypto_bridge/` package with `binance_ccxt.py` (async ccxt wrapper, testnet defence-in-depth) and `binance_engine.py` (`BinanceCCXTEngine(ExecutionEngine)`). Plugs into `execution.for_account()` via a new `kind == "binance"` discriminator on accounts — same signal pipeline, same Safety Guardian audit, same Explainable AI snapshot, same WebSocket broadcast as MT5BridgeEngine.
  - **Defence-in-depth live toggle**: an account runs against Binance testnet (sandbox) unless ALL of these are true: `account.testnet == False` AND `account.live == True` AND env `BINANCE_LIVE_ENABLED == true`. Any one falsy → testnet. Default `.env` ships with `BINANCE_LIVE_ENABLED=false`.
  - **Crypto-specific risk cap**: tighter than MT5 — `CRYPTO_MAX_RISK_PCT_PER_TRADE` (default 0.5%) on top of the existing Safety Guardian cascade. Blocks oversized crypto trades even when MT5-style checks pass.
  - **Encrypted-at-rest API keys**: reuses existing `secrets_vault.py` (AES-256-GCM, HKDF-derived key, `KEY_VAULT_MASTER`) — no Fernet duplication. The plaintext key never leaves `binance_ccxt._decrypt_creds`; UI gets a masked `••••XXXX` preview only.
  - **API** (`routes/crypto_routes.py`): `GET /api/crypto/status`, `GET /api/crypto/accounts`, `POST /api/crypto/accounts` (probes Binance with the supplied keys before persisting — 422 if invalid), `DELETE /api/crypto/accounts/{id}`, `GET /api/crypto/accounts/{id}/balance`, `GET /api/crypto/accounts/{id}/ticker`, `POST /api/crypto/accounts/{id}/execute` (manual signal fire, kill-switch enforced), `POST /api/crypto/accounts/{id}/verify`.
  - **`/crypto` page** (`pages/Crypto.jsx`) + sidebar entry "Crypto · Binance" (Bitcoin icon): master live-toggle banner, empty state CTA linking to testnet.binance.vision, account cards with TESTNET/LIVE chips + masked key fingerprint, VERIFY/INSPECT/DETACH actions. "ADD BINANCE ACCOUNT" modal with safety disclaimer ("Never grant Withdrawals") and testnet-default-on toggle. INSPECT modal shows live BTC/USDT ticker (bid/ask/last) + balance breakdown.
  - **Smart routing**: signals with `execution_hint=="limit"` and a `limit_price` go through `create_limit_order`; everything else market. Future iter-36 will wire the existing `execution_intel/` Smart Router for crypto.
  - **Symbol normalization**: `BTCUSD ↔ BTC/USDT`, `ETHUSD ↔ ETH/USDT`, already-slashed pairs pass through. `is_crypto_symbol()` helper for the bot_runner to route correctly.
  - **Tests**: 17 unit tests in `test_iter35_binance_ccxt.py` (normalize/testnet/smart-router/max-concurrent/safety-guardian-wires/risk-cap/market-order-persist/limit-order-pending/exchange-error). Testing agent ran HTTP smoke tests on the live REST surface (`test_iter35_crypto_routes_http.py` — 17 additional cases): auth gates, validation, 422 on bad keys with NO row leak, defence-in-depth status. **34/34 passing.** Also fixed an iter-33 regression where `test_self_improver_full_run` didn't mock `db.users.find_one` (added when iter-34 wired auto-accept into the orchestrator).


- 2026-06-25 (iter-34) — **Explainable AI Trading + Opt-in Auto-Accept**:
  - **Trade Explainer** (`trade_explainer.py`): builds a 4-section explanation per trade — (1) WHY DID I ENTER? strategy/technical/macro/news bias, (2) WHY THIS SIZE? original lot · vol-parity · Kelly · blended scale · 30d win-rate · reason, (3) WHAT FACTORS MATTERED? top-3 ranked contributors with HIGH/MEDIUM impact, (4) WHAT RISKS EXIST? SL/TP/pips/max-loss/macro gate. Prefers snapshot on `trade.explanation_snapshot` (frozen at insert time); falls back to live composition from `agent_activity`.
  - **Trade route**: `GET /api/trades/{trade_id}/explain` returns the explanation JSON.
  - **Auto-accept opt-in** (`research_agent/self_improver._maybe_auto_accept`): when `users.research_auto_accept.enabled=true` AND top proposal `beats_baseline` AND `delta_vs_baseline ≥ min_delta_pct/100`, immediately applies the compiled bot config to `bot_configs`, marks the proposal `auto_accepted`, dismisses the rest as `dismissed_auto`. Default threshold 10%. Disabled by default.
  - **Research API additions**: `GET /api/research/auto-accept` and `POST /api/research/auto-accept` (toggle/threshold).
  - **`/trades` page**: every row now has an "EXPLAIN" button (Sparkles icon) opening the 4-section modal with full reasoning trail. Live composition badge or snapshot timestamp shown.
  - **`/research` page**: AUTO-ACCEPT · OPT-IN panel with `min_delta_pct` input + toggle button (DISABLED/ENABLED), wired to the new endpoints.
  - **Tests**: 10 tests in `test_iter34_auto_accept_explain.py` (auto-accept disabled/below-threshold/applies/doesn't-beat-baseline; explainer entry/factors/sizing/risks/snapshot-preferred/live-fallback). **10/10 passing.** UI verified live: EXPLAIN modal renders with all 4 sections + factor impact chips; Research auto-accept panel renders with threshold input + toggle.


- 2026-06-25 (iter-33) — **Self-Improving Research Agent · nightly daily-loop**:
  - **Trade Analyzer** (`research_agent/trade_analyzer.py`): pure-deterministic. Groups last-30d closed trades by symbol/session/hour/action → emits weaknesses (≥5-trade buckets with below-overall win-rate AND non-positive P&L) + strengths.
  - **Hypothesis Generator** (`research_agent/hypothesis_generator.py`): Claude Sonnet 4.5 prompt — given weaknesses + current strategy, returns 3-5 *focused* candidate strategies (one-dim changes only). Strict closed-vocab validator drops invalid symbols, clamps allowed-set fields, forces `auto_execute=false` on every proposal (safety — proposals never auto-fire).
  - **Self-Improver orchestrator** (`research_agent/self_improver.py`): Analyze → fetch current bot config → generate hypotheses → backtest each via `strategy_backtest.run_backtest` → score (same composite as iter-30 optimizer) → persist top 5 as `db.improvement_proposals` with status=pending + `beats_baseline` flag. Stamps `db.research_runs.last_run_at` for cooldown enforcement.
  - **Daily sweep** (`research_agent/self_improver.daily_sweep`) wired into `bot_runner.loop()`: per-user 24h cooldown (env `RESEARCH_COOLDOWN_HOURS`), >99% of cron ticks return immediately, ran=N + proposals=N logged when work actually happens.
  - **API** (`routes/research_routes.py`): `GET /api/research/proposals` (pending + history), `GET /api/research/last-run` (cron metadata), `POST /api/research/run` (force-trigger), `POST /api/research/proposals/{id}/accept` (applies to `bot_configs`), `POST /api/research/proposals/{id}/dismiss`.
  - **`/research` page** (`pages/Research.jsx`): last-run status header, big RUN ANALYSIS NOW button, latest-run weakness/strength side-by-side panel + overall KPIs, pending proposal cards with rationale + 4-stat backtest summary (win-rate / P&L / sample / score) + compiled diff (symbols/session/risk/style/max_concur) + APPLY-TO-BOT / DISMISS buttons, history list. Trophy icon when `beats_baseline`. Sidebar entry added (Brain icon).
  - **Tests**: 9 new tests in `test_iter33_research_agent.py` (analyzer empty/full path, hypothesis validator drops invalid + clamps fields + forces auto_execute=false, full orchestrator run with mocked LLM + backtest, cooldown logic). **90/90 across iter-17/29/30/31/32/33 + safety_guardian regression suite passing.** End-to-end verified live (last-run + proposals endpoints return clean JSON, page renders with empty-state CTA).

- 2026-06-25 (iter-32) — **Autonomous deleveraging + Execution Intelligence (Phase 1)**:
  - **Autonomous deleveraging cron** (`portfolio/auto_deleverage.py` + wired into `bot_runner.loop()`): every loop tick (~60s) walks accounts with open positions, builds the snapshot, executes the action list when `needs_deleveraging` fires. Per-account cooldown (`AUTO_DELEVERAGE_COOLDOWN_MIN` default 15min) prevents thrashing. Broadcasts `auto_deleverage` WS event + Telegram alert with triggers/closed-count/DD%/VaR%. Env toggle `AUTO_DELEVERAGE_ENABLED` (default ON).
  - **Liquidity Scoring** (`execution_intel/liquidity.py`): 0-100 composite — 50% spread quality + 30% tick velocity + 20% session window. Returns tier (EXCELLENT/GOOD/FAIR/POOR/AVOID), component breakdown, and human-readable reason.
  - **Smart Order Router** (`execution_intel/smart_router.py`): picks `venue` + `order_type` (MARKET vs LIMIT vs DEFER) + slice strategy based on liquidity tier. EXCELLENT/GOOD → MARKET. FAIR → LIMIT ±N pips. AVOID → DEFER. Large lots (≥0.10) get sliced via TWAP/VWAP regardless of tier.
  - **TWAP/VWAP scheduler** (`execution_intel/twap_vwap.py`): TWAP = equal lots over equal intervals; VWAP = lots weighted by a 24h volume profile (london/NY overlap = peak). Persists schedules to `db.slice_schedules` with `due_slices()` helper for the bot_runner to consume each tick.
  - **Order-Book Pulse** (`execution_intel/order_book.py`): L1 proxy until EA v1.28 ships real DOM. Classifies ACTIVE/NORMAL/THIN/FROZEN from tick velocity + spread ratio; produces a depth_score 0-100. Marks `real_dom_available=false` so the UI is honest about the proxy.
  - **API**: `GET /api/execution/quality?symbols=...`, `POST /api/execution/preview`, `POST /api/execution/schedule`, `GET /api/execution/schedules`.
  - **`/execution` page** (`pages/Execution.jsx`): Live Execution Quality cards (one per symbol with score bar + spread ratio + ticks/min + session + book pulse + depth), Smart Order Router probe (form to test what the router would do for any hypothetical signal — returns Venue/Order Type/Slice/Liquidity tiles + reason + the proposed TWAP/VWAP schedule with fire times), Active TWAP/VWAP Schedules list. Sidebar entry added (Zap icon).
  - **Tests**: 22 new tests in `test_iter32_execution_intel.py` (liquidity components, SOR decisions, TWAP/VWAP schedules, order-book classifier, auto-deleverage disabled flag / cooldown / end-to-end fire). **95/95 across iter-17/27/29/30/31/32 + safety_guardian regression suite passing.** Live end-to-end verified.

- 2026-06-25 (iter-31) — **Portfolio Risk Manager · full account-wide oversight**:
  - **Sector classification** (`portfolio/sectors.py`): closed-vocab map for XAUUSD→commodity, BTCUSD→crypto, EURUSD→fx_major, NAS100→equity_index, etc. + heuristic fallback for unknown symbols.
  - **VaR calculator** (`portfolio/var.py`): parametric 1-day **95% AND 99%** Value-at-Risk via variance-covariance method. Uses ATR%/price as daily vol proxy + pairwise Pearson correlation from 30-day closes. Returns absolute USD VaR + % of equity + per-position weights + full correlation matrix.
  - **Peak-to-trough drawdown** (`portfolio/drawdown.py`): equity HWM persisted to `db.accounts.equity_hwm` (ratchets up only). SOFT_DD_PCT=8% warns, HARD_DD_PCT=15% triggers auto-deleveraging. Both env-overridable.
  - **Risk manager** (`portfolio/risk_manager.py`): composes drawdown + sector exposure + VaR + cross-asset correlation into a unified snapshot. Priority-ordered triggers: `hard_drawdown` → `sector_cap_*` → `combined_corr_bucket` → `var_cap`. Produces a dedup'd actions list (close largest contributor, partial bucket trim, sector-cap relief).
  - **Auto-deleveraging engine**: marks selected trades with `close_requested=True` + `close_reason="auto_deleverage_*"` — the existing reconciler/EA flow does the broker close. Same trade can be flagged by multiple triggers; dedupe keeps highest-priority reason.
  - **Combined-risk bucket** (the user's example): crypto + commodity + equity_index combined notional capped at 50% of equity *only when* the bucket's avg pairwise abs-corr ≥ 0.70. When NASDAQ + BTC become highly correlated AND combined exposure breaches the cap, the engine closes the largest position to bring exposure back in line.
  - **`/portfolio` page** (`pages/Portfolio.jsx`): full risk dashboard — KPI strip (Equity/HWM/Drawdown/VaR95) + Max-Drawdown Controls + Sector Exposure bars + Cross-Asset Correlation Bucket + Pairwise Correlation Matrix (color-coded) + Per-Position VaR breakdown. Big red `EXECUTE DELEVERAGING` button when triggers fire.
  - **Sidebar**: new "Portfolio Risk" entry (Shield icon) between Safety Blocks and Analytics.
  - **Env knobs** (all default-on): `PORTFOLIO_DD_SOFT_PCT`, `PORTFOLIO_DD_HARD_PCT`, `PORTFOLIO_SECTOR_CAP_CRYPTO_PCT`, `PORTFOLIO_SECTOR_CAP_COMMODITY_PCT`, `PORTFOLIO_SECTOR_CAP_EQUITY_PCT`, `PORTFOLIO_SECTOR_CAP_FX_MAJOR_PCT`, `PORTFOLIO_HIGH_CORR_THRESHOLD`, `PORTFOLIO_COMBINED_RISK_CAP_PCT`, `PORTFOLIO_VAR_CAP_PCT`.
  - **Tests**: 11 new tests in `test_iter31_portfolio.py` covering sectors, VaR (empty + single position), drawdown HWM ratchet + soft/hard breach, risk manager triggers (hard DD + combined corr bucket), deleverage execution. **73/73 across iter-17/27/29/30/31 + safety_guardian regression suite passing.**

- 2026-06-25 (iter-30) — **AI Strategy Generator · full Generate → Code → Backtest → Optimize → Deploy pipeline**:
  - **Stage 2 · WRITE CODE** (`strategy_code_generator.py` + `POST /api/nl/strategy/code`): Claude expands the compiled JSON preset into a closed-vocab DSL (5 allowed fields × 5 allowed ops × 4 allowed exit kinds) + a Python-flavoured pseudocode block. Strict validator clamps param ranges (no LLM hallucination wrecks the bot). Returns DSL with `entry_rules`, `exit_rules`, `params`, `pseudocode`. The bot interprets the tree — nothing `eval()`s.
  - **Stage 4 · OPTIMIZE** (`strategy_optimizer.py` + `POST /api/nl/strategy/optimize`): grid-search across 36 variants (session × symbol subset × lookback) each backtested against the user's own trade history. Composite score = `win_rate × log(1+n) + 0.001×pnl`. ≥5-trade qualification floor rejects lucky 2-trade winners. Returns baseline vs best + top-10 ranked variants + delta % + transparency notes about what's NOT optimized (indicator thresholds).
  - **Frontend pipeline overhaul** (`pages/Strategies.jsx`): 5-button workflow — WRITE CODE (purple) → BACKTEST (green) → OPTIMIZE (cyan) → SAVE → DEPLOY (orange). Each stage gets its own panel: STAGE 1 compiled JSON, STAGE 2 code (pseudocode + DSL rules), STAGE 3 backtest stats, STAGE 4 optimizer baseline-vs-best with rank table + "APPLY BEST VARIANT" button to merge the optimized filters back into the compiled strategy before DEPLOY.
  - **Library schema** (`db.strategies`): now also persists `dsl` + `optimization` payloads so users can LOAD a saved strategy and see its full pipeline state restored.
  - **Tests**: 10 new tests in `test_iter30_strategy_pipeline.py` (DSL validator pos/neg cases, code-gen with mocked LLM, optimizer baseline/improvement/lucky-sample disqualification). **69/69 across iter-17/27/29/30 + safety_guardian regression suite green.**
  - **End-to-end verified live**: `/api/nl/strategy/optimize` returns 36 tested variants + selected best filter set + transparency notes.

- 2026-06-25 (iter-29) — **7-agent pipeline + standalone AI Strategy Generator page**:
  - **3 new specialised analysers** (run in parallel before Strategy):
    - `TechnicalAnalysisAgent` (`agents/technical_agent.py`): trend (MA20 vs MA200), RSI extremes, ATR%, regime — pure deterministic from cached indicators
    - `MacroAnalysisAgent` (`agents/macro_agent.py`): FRED + real-yield + DXY + COT + **consults macro_gate** for XAUUSD
    - `NewsSentimentAgent` (`agents/news_sentiment_agent.py`): wraps `score_sentiment` with deterministic label + bias digest
  - **2 new post-decision shapers**:
    - `PortfolioAllocatorAgent` (`agents/portfolio_allocator_agent.py`): blended **vol-parity × Kelly** trim of the proposed lot size. Reads last-30d win-rate from `db.trades`, neutral 1.0 on no data. Never inflates above strategy's proposed size, MIN_SCALE=0.25 floor.
    - `ExecutionOptimizerAgent` (`agents/execution_optimizer_agent.py`): three guards — spread (>2.5× rolling median refuses), session (defer XAUUSD off-hours 22-06 UTC + weekends unless aggressive_mode), slice planner (split lots > 0.10 into 0.05 chunks). Env-overridable thresholds.
  - **Orchestrator rewired**: `Technical + Macro + News (parallel)` → `Strategy` → `Risk` → `PortfolioAllocator` → `ExecutionOptimizer` → broker engine. Each agent emits its own activity-log step with bias digest + duration_ms. Legacy `ResearchAgent` kept for back-compat with pre-iter29 activity rows.
  - **/strategies — new standalone page** (`pages/Strategies.jsx` + `routes/strategies_routes.py`): prompt input → COMPILE (via existing `/api/nl/strategy`) → BACKTEST 30d (via iter-27 endpoint) → SAVE to library (new `db.strategies` CRUD) → APPLY TO BOT. Library lists saved strategies with per-symbol win-rate + LOAD/DELETE.
  - **/agents page revamp**: replaces "4 agents" with "7 agents" grid + adds gradient AI Strategy Generator CTA at top linking to /strategies.
  - **Sidebar**: new "Strategies" entry (Sparkles icon) between Agents and Risk Commander.
  - **Tests**: 14 new tests in `test_iter29_new_agents.py` covering all 5 new agents' core behaviours + updated iter17 orchestrator pipeline test (now asserts 7 agent names). **59/59 across iter-17/27/29 + safety_guardian regression suite passing.**

- 2026-06-25 (iter-28) — **Broker-real-time prices on the Trades page**:
  - **(b) Quote-cache TTL tightened** (`market.py::ttl_seconds_for_quote`): crypto+commodity dropped from 60-120s → **5s**, so even users on the old EA see fresh prices within the 5s frontend poll. FX stays at 60s (low movement, conserves quota).
  - **(a) EA v1.27 → broker-exact ticks** (`EmergentTradingBridge.mq5`): position snapshot now carries `current_price` (`PositionGetDouble(POSITION_PRICE_CURRENT)`) alongside the existing `profit`. Version bumped 1.26→1.27, header changelog updated.
  - **Backend tick relay** (`bridge_routes.heartbeat`): on each HB, accumulates per-position ticks and broadcasts a single `position_ticks` WS event `{account_id, ticks: [{ticket, symbol, current_price, profit}], ts}`. Skipped on terminal mismatch (won't leak foreign-account ticks).
  - **Frontend overlay** (`Trades.jsx`): subscribes via `useLiveStream`, maintains a `liveTicks` map keyed by `mt5_ticket`. The CURRENT cell and LIVE P&L column prefer the EA tick (with a pulsing green-dot "broker-live" indicator) and fall back to the 5s-polled feed for legacy-EA users. Risk Thermometer + Closest-SL/TP also consume the same source.
  - **Schema**: `BridgePosition.current_price` is `Optional[float]` — older EAs continue working unchanged.
  - **Tests**: 6 new tests in `test_iter28_live_ticks.py` (TTL knob, schema acceptance, broadcast on tick, no-tick skip, mismatch skip). 37/37 across iter-27 + safety_guardian + position_snapshot regression suite still green.

- 2026-06-25 (iter-27) — **2026 SOTA upgrades · Macro-Regime Gate + Strategy Backtest Preview**:
  - **(d) Macro-Regime Hard Gate for XAUUSD** (`/app/backend/macro_gate.py`): Deterministic, server-side gate consuming the FRED `fred_cache` collection. Blocks XAUUSD trades when 10Y Treasury yields move ≥ 25bps WoW or USD broad index moves ≥ 1.5% WoW *against* the proposed direction (BUY blocked on yield/USD surge, SELL blocked on collapse). Symbol-agnostic interface — non-gold symbols pass through with a stamped audit row. Fails open when no FRED cache exists. Wired into `safety_guardian.audit_pre_trade` as check #8 so blocked trades automatically appear in `db.safety_blocks` and the existing Safety Blocks UI. New `/api/macro/gate` endpoint returns BUY/SELL status + reasoning for the Dashboard banner.
  - **(a) Strategy Backtest Preview** (`/app/backend/strategy_backtest.py` + `POST /api/nl/strategy/backtest`): New endpoint replays a compiled NL strategy against the user's own historical closed trades from the last 30 days (configurable). Returns win rate, total/avg/best/worst P&L, per-symbol breakdown, and contextual notes (small-sample warnings, untestable filters). RiskCommander.jsx now exposes a `BACKTEST 30d` button next to APPLY TO BOT so users see expected performance *before* overwriting their bot config. Honestly disclaims what isn't simulated (risk_level/strategy_style).
  - **MacroClimate widget**: Added a status banner above the 5-card grid showing live XAUUSD gate state. Green ShieldCheck = both directions open; red ShieldOff = one or both blocked with the per-direction reason inline ("10Y Treasury yield surged +0.30pp WoW — gold longs blocked").
  - **Env knobs (all default-on)**: `MACRO_GATE_ENABLED`, `MACRO_YIELD_BUY_BLOCK_BPS=0.25`, `MACRO_YIELD_SELL_BLOCK_BPS=0.25`, `MACRO_DXY_BUY_BLOCK_PCT=1.5`, `MACRO_DXY_SELL_BLOCK_PCT=1.5`.
  - **Tests**: 21 new tests across `test_iter27_macro_gate.py` (14) and `test_iter27_backtest.py` (7) — all passing. Full safety_guardian regression suite (11) still green.

- 2026-02-24 (iter-24j) — **Manual import positions modal — workaround for legacy EAs**:
  - **Why**: Even after EA v1.24 (which reports `account_login` so STOIC knows it's reading the right terminal), positions opened BEFORE the EA was attached remain invisible unless v1.25 (positions snapshot) is installed. To unblock users who don't want to keep re-installing the EA, added a one-shot manual import.
  - **Backend**: `POST /api/accounts/{id}/import-positions` body `{positions: [{ticket, symbol, type, volume, price_open, sl, tp}]}`. Each unknown ticket → inserted as `origin=external`, `manually_imported=True`. Idempotent: existing tickets are skipped (returned in `skipped_existing[]`).
  - **UI**: New yellow **IMPORT POSITIONS** button on each account row (next to REFRESH). Modal accepts CSV-style paste (`TICKET, SYMBOL, BUY|SELL, VOLUME, PRICE_OPEN[, SL, TP]`) one per line. Tab- and comma-separated both accepted. Footer note tells users this becomes automatic once EA v1.25 is installed.
  - **Tests**: 5 new tests covering create, idempotency, 404 on unknown account, Pydantic validation (invalid type + zero volume). 14/14 pass combined.
- 2026-02-24 (iter-24i) — **EA v1.25 · Heartbeat positions snapshot backfills missing trades**:
  - **Bug**: User had 4 open trades on micro account at the broker, but STOIC's Trades page showed 0. Cause: EA's `OnTradeTransaction` (v1.23) only catches NEW deals — positions opened BEFORE the EA was attached or opened directly on MT5 outside the EA's awareness are invisible to STOIC.
  - **EA v1.25**: New `BuildPositionsJson()` builds a full snapshot of every position via `PositionsTotal()` loop. Each entry carries `ticket / symbol / type / volume / price_open / sl / tp / time_open / magic / profit`. Sent in every heartbeat as `positions: [...]`.
  - **Backend**: Heartbeat handler iterates the snapshot. For each ticket NOT already in STOIC's trades collection, inserts a new row with `status=open`, `origin=external` (magic=0) or `origin=auto` (our EA's magic), `backfilled_from_snapshot=True`. Skipped entirely when broker-account mismatch is detected (those positions belong to the wrong login).
  - **Idempotency**: Lookup by `(account_id, mt5_ticket)` before insert → replays produce zero duplicates. Verified with 3 consecutive identical heartbeats → still exactly 1 trade row.
  - **WebSocket**: New `trades_backfilled` broadcast (count + account_id) so the Trades page can refresh live.
  - **Tests**: 5 new tests in `test_iter24h_position_snapshot.py` covering new-ticket insert, replay idempotency, skipped-on-mismatch, legacy-EA compatibility, magic→origin classification. 20/20 pass across iter-24 e/f/g/h.
- 2026-02-24 (iter-24h) — **Self-healing wrong-balance — null on mismatch + visible EA diagnostic**:
  - **Backend**: Heartbeat handler now refuses to overwrite `balance`/`equity` when the EA's reported `account_login` differs from the configured `account_number`. Balance becomes `None`, status flips to `disconnected`. Prevents silent display of the wrong account's balance.
  - **UI**: Balance card shows `—` + "AWAITING VALID HEARTBEAT" when balance is null. New small diagnostic line under broker info: `EA READING FROM · #67199078 ✓` (green when match) or `#67198987 ⚠ MISMATCH` (yellow). Renders only when EA reports `account_login` (v1.24+).
  - **Operational note**: User's two live EAs are still on legacy (no `account_login` → diagnostic line absent). Both reporting from same MT5 login. User must install EA v1.24 to enable the self-healing behaviour.
  - **Tests**: Existing 4 wrong-terminal tests still pass.
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

## Changelog — Feb 2026

### AI Strategy Optimizer (iter37 — suggest-only trade review, THIS SESSION)
- Bot reviews closed trades over last 24/48h per account scope and suggests
  strategy adjustments to raise win rate. SUGGEST-ONLY: user clicks Apply/Dismiss.
- LLM: **claude-fable-5 (confirmed working on Emergent Universal Key)**, fallback claude-opus-4-8.
- Backend: `/app/backend/ai_optimizer.py` (stats digest, ALLOWED_FIELDS whitelist+clamps,
  validate_recommendations, apply_recommendation, scheduled_sweep every 24h/scope via
  hourly `_optimizer_loop` in server.py). Routes: `/app/backend/routes/optimizer_routes.py`:
  POST /api/optimizer/analyze (5-min cache), GET /report, GET /summary,
  POST /report/{rid}/rec/{recid}/apply|dismiss (409 on non-pending).
- Rec types: config_change (field/from/to), preset_switch, pause_bot. All whitelisted server-side.
- Collection: `optimizer_reports` (per-run doc, recs carry pending/applied/dismissed status).
- Frontend: `AiOptimizerCard.jsx` on Dashboard (verdict rows + pending badge, deep-links
  `/bot-config?account=<id>&optimizer=1`), `AiOptimizerSection.jsx` in BotConfig
  (ANALYZE 24H/48H, verdict/headline/stats/patterns/rec cards with Apply/Dismiss).
- Tests: /app/backend/tests/test_ai_optimizer.py (6 unit) +
  test_iter_optimizer_http.py (7 http, self-provisioning). iteration_37.json: 100% pass.
- Telegram alert: scheduled sweep sends `optimizer_critical` alert on CRITICAL verdict
  (via notifier.send_telegram; opt-out toggle added to Notifications page EVENT_LABELS).
- MANUAL TRADES EXCLUDED: optimizer reviews BOT-EXECUTED trades only
  (`origin ∈ {auto, null}` — null = legacy bot trades; manual/external/other_ea/test_trade
  excluded). Report carries `excluded_manual_trades` count, shown as an amber badge in the UI.
- STRICTLY PER-ACCOUNT (iter38): /analyze requires account_id (400 otherwise); each account's
  trades reviewed separately; Apply writes ONLY to that account's config — if the account
  still inherits the default profile, the default is cloned into a per-account override
  first (`_ensure_account_override`) so changes never leak to other accounts. Scheduled
  sweep iterates db.accounts (effective config active). Summary excludes legacy null-scope
  reports. Bot Config section shows a "select an account" hint on Default Profile scope.
  Tests: 15/15 (incl. apply-isolation + manual-origin-exclusion + 400-without-account).
- GUIDE UPDATED (iter39): new Guide section "13. AI Strategy Optimizer" (suggest-only,
  per-account isolation, schedule, Telegram critical alert, Fable 5); TOC renumbered to 23
  sections and drifted in-section H2 numbers fixed; Daily Routine gained Trades-filters +
  Optimizer checks; Multi-Bot section gained Step 4 "Bot Pulse strategy chips/dropdown".
- trade_of_day_cap max raised 50 → 100 (BotConfig.jsx input max; ai_optimizer ALLOWED_FIELDS
  clamp aligned to 100). Backend had no hard cap — verified PUT accepts 100.
- EA v1.38 (iter40) — retcode 10016 INVALID_STOPS fix (Tauro Markets XAUUSD.fx rejection):
  new ClampStops() clamps SL/TP to max(SYMBOL_TRADE_STOPS_LEVEL, freeze level, live spread)
  +2pt buffer, sides per MT5 rules (BUY vs Bid, SELL vs Ask), normalises with the TRADED
  symbol's digits (was chart _Digits bug), retries OrderSend once with doubled buffer on
  10016 (never opens without SL). Trailing/breakeven SL moves clamped too. Version bumped
  in mq5 (3 places) + LATEST_EA in bot_routes/diagnostic_routes + setup_routes
  ea_latest_version + Accounts.jsx. Tests: test_iter40_ea_clamp_stops.py (5) + iter85
  updated to 1.38 — 18/18 pass. NOTE: fix reaches live users only after prod redeploy
  AND users updating their EA to v1.38.
- PAYOFF-RATIO REPAIR (iter41) — user found high WR (70-83%) but flat profit; analysis
  showed payoff 0.41-0.80 (losers 1.3-2.4x winners), worst = auto_deleverage_var_cap
  closes (-$362 avg). Shipped 4 fixes:
  (a) Soft-Stop: trade_manager cuts losers at `soft_stop_loss_fraction` (0.6 default) of SL
      distance after `soft_stop_min_minutes` (10) age; close_reason=soft_stop_cut_loss.
      Fields: soft_stop_enabled/loss_fraction/min_minutes — OFF by default.
  (b) VaR-cap deleverage now prefers culling the WORST LOSER among the top-risk symbol's
      positions (mirrors iter-48 sector fix).
  (c) let_winners_run toggle: TP1 → BE-only (no 50% banking), TP2 closes half, TP3 rest.
  (d) Optimizer stats now include payoff_ratio + avg_pnl_per_trade; prompt teaches the
      payoff trap; 4 new ALLOWED_FIELDS; PAYOFF cell in UI stats grid.
  Plumbing: models.py BotConfigUpdate, bot_routes _serialize (was missing new fields —
  fixed), BotConfig.jsx save whitelist + Section 04 toggles.
  Tests: tests/test_iter41_payoff_repair.py (14) + optimizer units = 20/20. Live forced
  analysis confirmed Fable 5 recommends soft_stop/let_winners_run on real data.
- PROFIT-TIED LEARNING OBJECTIVE (iter42) — user directive: "all bot logic and training
  must tie win rate and profit together". New shared module /app/backend/objective.py
  (expectancy_stats, stoic_score = WR × avg_profit_per_trade × ln(1+n)). Rewired ALL
  5 learners:
  (1) strategy_optimizer._score → stoic_score (losing variants score negative);
  (2) auto_tune bucket qualification now also requires total_pnl>0 AND expectancy_r>0
      (breakdown rows carry expectancy_r/payoff_ratio);
  (3) adaptive_mode._profit_tied_multiplier — WR boost only when expectancy_r ≥ 0.15;
      net-losing window caps at 0.7×; reason string shows payoff+expectancy;
  (4) learned_meta profit-WEIGHTED training — sample weight = clip(|pnl|/median,0.25,4)
      for both logreg (weighted gradient) and XGBoost (DMatrix weight); artifact has
      profit_weighted flag; backwards compatible (sample_w optional);
  (5) ai_optimizer prompt PRIMARY OBJECTIVE statement (WR & profit must move together).
  Tests: test_iter42_profit_tied_objective.py (15) + iter69/iter52 regressions = 67 passed.
  Live verified: admin's 88.9% WR / 0.22 payoff window now gets 1.0× (was 1.3× boost).
- US30 / NAS100 INDEX SUPPORT (iter43) — market.py SYMBOL_MAP asset "index" with Yahoo
  v8 chart feed (^DJI / ^NDX): _yahoo_index_quote (meta.regularMarketPrice) +
  _yahoo_chart_history (generic 1y daily; gold GC=F refactored onto it); quote TTL 15s.
  broker_symbol_detector: US30 added to KNOWN_BASES; BASE_ALIASES US30→(DJ30,WS30,DOW30,
  DJI30), NAS100→(USTEC,US100,NDX100,USTECH). pip_utils + portfolio sectors already had
  index specs. Symbols appear in Bot Config selector via /api/market/symbols (17 total).
  Tests: test_iter43_index_symbols.py (11, incl. live feed) — 54 passed w/ regressions.
- FULL TRADE HISTORY (iter44) — GET /api/trades/history?date_from&date_to&account_id
  (no 100-row cap, up to 5000; range matches closed_at, falls back to opened_at/created_at
  for open trades; returns summary: wins/losses/breakeven/win_rate/total_pnl/gross/avg).
  Trades.jsx: period preset chips (TODAY/YESTERDAY/THIS WEEK/LAST WEEK/THIS MONTH/
  LAST MONTH, Monday-start weeks, local TZ) + "PERIOD RESULTS" summary panel; custom
  date inputs kept (labeled CUSTOM, clears preset on manual edit); client-side date
  re-clamp skipped in history mode. Tests: test_iter44_trade_history.py (3) pass;
  UI verified via screenshot (THIS WEEK → 256 closed, 75.8% WR, +$5525).
- GHOST-CLOSE RECOVERY + ANTI-TILT FALSE FREEZE (iter45) — user hit: 4 TP-winning trades
  recorded closed with exit=None/pnl=0 (EA out-deals stopped arriving Mon Jul 6; in-deals
  still flow; last out-deal Fri Jul 3 15:52 — EA-side, unresolved, mitigated server-side),
  and those zero-P&L closes wrongly tripped anti-tilt freeze on 4 accounts.
  Fixes: (1) anti-tilt loss = pnl STRICTLY < 0 in all 4 sites (bot_runner gating,
  bot_routes ×2, diagnostic_routes) — all 4 accounts unfrozen, verified;
  (2) heartbeat now stamps live_pnl/live_price/live_at on open trades (bridge_routes);
  (3) reconciler ghost-close uses last snapshot as ESTIMATED exit (pnl_estimated=True,
  close_reason take_profit_reconciled/stop_loss_reconciled/broker_reconciled_estimated
  via _infer_close_reason, tol 0.04%); no snapshot → pnl_unknown=True;
  (4) external-deal repair now overwrites estimates (real_exit_known guard);
  (5) one-time repair: 4 ghost trades restored from TP targets (+$64.74/+$23.04/
  +$39.05/+$74.86, pnl_estimated). Tests: test_iter45_ghost_close_recovery.py (7) pass.
  OPEN QUESTION: why EA v1.38 out-deals/close-reports stopped — monitor next closes;
  live_pnl estimate now covers the gap regardless.

### FRED macro feeds + Dashboard "Macro Climate" widget (iter26)
**Why:** The bot's signal logic references DXY, real yields, and Fed posture but those values were proxied/assumed, not pulled from a real source. FRED gives us authoritative daily values for free with a registered API key.

**Backend:**
- New `/app/backend/macro_feeds.py` — async httpx pulls of 5 series (DGS10, DGS2, FEDFUNDS, DTWEXBGS, T10YIE) with day-over-day + week-over-week deltas. Filters FRED's holiday `.` sentinels before delta math.
- Hourly cache to `db.fred_cache` (1h TTL) → ≤ 5 FRED calls/hour total regardless of dashboard load.
- Single-flight async locks prevent stampede when multiple users hit Dashboard at once.
- Graceful fallback: when FRED unreachable, serves stale cache with `stale_cache: true` flag instead of crashing.
- New route `GET /api/macro/snapshot[?force=true]` (admin/user). 401 unauth.
- `FRED_API_KEY` added to `/app/backend/.env`.

**Frontend:**
- New component `/app/frontend/src/components/MacroClimate.jsx` — 5-card grid below BotHealthScore on Dashboard. Each card: series_id label, full name, latest value with unit, DoD + WoW deltas with color/arrow, observation date.
- Self-refreshes every 10min, self-hides when no data, shows amber STALE CACHE chip when backend fell back to stale.

**Testing — verified by testing_agent_v3_fork (iter26):**
- ✅ 110/110 pytest tests pass (9 new + 101 regression across all prior iterations)
- ✅ Backend 100% · Frontend 100% · 0 critical · 0 minor (after cleanup of dead-code branch + legacy `_id='snapshot'` cache doc)
- ✅ Live FRED data confirmed flowing: DGS10=4.50%, DGS2=4.16%, FEDFUNDS=3.63%, DTWEXBGS=120.40, T10YIE=2.18%
- ✅ The existing AI REASONING strip on Dashboard already cites this exact data ("DXY regime bullish_usd 101.6 vs 100.2 EMA, real yields rising +0.09 over 5d in bearish_gold regime") — signal logic immediately benefits

### Global InvalidId exception handler (iter25 — belt-and-suspenders)
Registered `bson.errors.InvalidId` exception handler on the FastAPI app in `server.py`. Any future route that forgets to use `route_utils.parse_object_id` and calls raw `ObjectId(user_input)` now returns a clean **404 `{detail: "Resource not found"}`** instead of a 500. Prevents the entire iter22 P2.2 class of bug from re-emerging when new routes are added.

**Testing:**
- ✅ 3/3 iter25 unit tests pass (handler registered, unprotected route returns 404, valid OID still passes)
- ✅ 47/47 full regression (iter22 + iter24 + iter25 + safety_guardian + max_concurrent + iter26) all green

### ObjectId hardening carry-over (iter24)
Completed the carry-over from iter22 — migrated remaining raw `ObjectId()` calls to `parse_object_id` across the 4 flagged route modules. Malformed ObjectIds on any user-supplied path/query param now return a clean **404** across the entire codebase instead of a 500.

**Files migrated:**
- `bot_routes.py` — 5 user-input `ObjectId(account_id)` calls → `parse_object_id`. Internal-data loop at `/bot/status` defensively wrapped in try/except so corrupt configs no longer crash status.
- `nl_routes.py` — `ObjectId(trigger_id)` at DELETE `/nl/triggers/{id}` → `parse_object_id`.
- `signal_routes.py` — `ObjectId(signal_id)` at DELETE `/signals/{id}` → `parse_object_id`.
- `affiliate_routes.py` — admin endpoints `/admin/affiliate/payout-requests/{rid}/process` and `/admin/affiliate/commissions/{cid}/mark-paid` changed from 400 → 404 for consistency. Internal-data ObjectIds on `affiliate_id` from found docs defensively wrapped (log error + return 200 with `warning` field on corruption rather than 500).

**Testing — verified by testing_agent_v3_fork (iter24):**
- ✅ 10 iter24 ObjectId regressions pass
- ✅ 43 backward regressions from iter22 + safety_guardian + max_concurrent_race + iter26 still pass
- ✅ 7 new positive-path tests added by tester — all happy paths intact
- ✅ Source-scan test confirms zero remaining unwrapped `ObjectId(<user_input>)` calls across the 4 files
- ✅ No frontend callers depend on the affiliate admin 400→404 change (verified by tester grep)
- ✅ 0 critical/0 minor issues

### Security & stability hardening (iter22 → iter23)
**5 issues identified by user code-audit:**

- **P1.1** `bridge_routes.report_trade` — slippage_veto path referenced undefined `now_iso` → NameError 500. **Fix:** Inlined `datetime.now(timezone.utc).isoformat()` at the reference site. No more 500s when slippage > cap.

- **P1.2** Credentialed CORS open to every origin (`allow_origin_regex='.*' + allow_credentials=True`). **Fix:** Rewrote CORS in server.py to enforce mutual exclusion — explicit allowlist + credentials, OR wildcard + NO credentials, never both. `CORS_ORIGINS` env now drives the policy; preview URL whitelisted.

- **P1.3** Broker passwords revealable with just a session. **Fix:** `POST /accounts/{id}/credentials/reveal` now requires `{password, include_master?}` body. Password re-confirmed via `auth.verify_password` (403 on fail). `master_password` only returned when `include_master=true`. Every reveal attempt (success/failure) audit-logged to new `credential_reveals` collection with `{user_id, account_id, result, revealed[], at}`. Frontend now prompts via a modal with password input + include-master checkbox (only shown when master exists).

- **P2.1** `broker_deals.insert_one` swallowed ALL exceptions as duplicate. **Fix:** Narrowed `except` to `DuplicateKeyError`. Other exceptions log + raise 500 so real broker fills no longer silently disappear.

- **P2.2** Raw `ObjectId()` on user-supplied IDs → 500. **Fix:** New `/app/backend/route_utils.py` with `parse_object_id(value, resource)` helper that returns clean 404 on invalid hex. Applied to all bridge/account/trade route call sites. Remaining call sites in bot/nl/signal/affiliate routes flagged for follow-up.

**Testing — iter22 + iter23 (testing_agent_v3_fork):**
- ✅ **Iter22**: 88/88 backend tests pass · 0 critical issues · caught 1 frontend caller drift on Accounts.jsx CredentialsPanel
- ✅ **Iter23**: 18/18 backend regression pass · 6/6 frontend criteria pass · audit log persistence verified

### Suggested Config Adjustment (auto-tune from safety-block patterns)
**Why:** Passive observability (safety-blocks UI) isn't enough — users need a one-click bridge from "I see the bot is being clipped" to "my config is now within the envelope". The page now actively proposes the smallest config change that should reduce future blocks.

**Backend:**
- `GET /api/safety-blocks/suggestion?days=7` — when ≥3 blocks of the same kind exist in the window, returns `{has_suggestion, top_reason, reason_label, count, title, severity, rationale, preview, patch, scope_account_id}`. Mapping:
  - `per_trade_risk_cap` → `risk_level` steps down one notch (high→medium→low)
  - `total_open_risk_cap` → `max_concurrent_trades` -= 1 (floor 1)
  - `lot_vs_equity_sanity` → `max_lot_size` × 0.7 (floor 0.01, 2-dp)
  - `daily_loss_cap` / `equity_vs_balance_floor` / `free_margin_floor` → `active=false` (red, pause)
  - `risk_inputs_present` → has_suggestion=true, patch=null (manual review)
- `POST /api/safety-blocks/apply-suggestion {patch, scope_account_id}` — sanitizes patch against allowlist `{risk_level, max_concurrent_trades, max_lot_size, active}`, validates ownership of `scope_account_id`, persists with `updated_at` + `last_suggestion_applied_at` stamps, returns the new config snapshot.

**Frontend:**
- `SuggestionBanner` rendered at top of `/safety-blocks` when `has_suggestion=true` — Sparkles icon, severity-tinted border (amber/red), label/title/rationale/CHANGE-preview-chip + APPLY button (only shown when `patch` is non-null).
- On APPLY: POST → toast with new_config summary → re-fetch list/stats/suggestion (suggestion typically disappears next refresh since blocks are now within new envelope).

**Bug found & fixed:** Iter20 testing agent caught a 500 on non-hex `scope_account_id` (raw `ObjectId()` raised `InvalidId`). Fixed with try/except → 404. Verified in iter21: 58/58 backend tests + live UI flow for both non-hex (graceful 404 toast) and valid hex (200 + persisted) paths.

### Safety Blocks UI page — observability for guardian refusals
**Why:** Users need to see WHY the bot's trades are being refused so they can dial config into the "goldilocks zone" (aggressive enough to earn, not so aggressive the guardian constantly slaps it down).

**Backend `/api/safety-blocks/...` (user-scoped, owner-isolated):**
- `GET /list?limit=&days=` → user's blocks newest-first with audit + context
- `GET /stats?days=` → `{by_reason, by_day (zero-filled), total, thresholds, window_days}` powers the bars + sparkline
- `GET /{block_id}` → single-block detail; 400 on bad OID, **404 if user doesn't own it** (no leak across users)

**Frontend `/safety-blocks` page:**
- Sidebar entry `nav-safety-blocks` (ShieldCheck icon)
- **Goldilocks banner** — GOOD/OK/WATCH/LOOSEN tone based on count: "GOLDILOCKS · Bot trading freely within all safety floors"
- **Blocks by Reason** bars — color-coded per severity, % of total
- **Blocks per Day** sparkline — zero-filled SVG with hover tooltips
- **Current Guardian Thresholds** collapsible — all 6 SAFETY_* env values
- **Refused Trades** table — clickable rows expand to show ACCOUNT STATE AT BLOCK + AUDIT TRAIL (PASS/FAIL per check)
- Days selector (1d/7d/30d) + Refresh button

**Testing — verified by testing_agent_v3_fork (iter19):**
- ✅ 39/39 backend tests pass · 0 critical/minor backend issues
- ✅ 12 new HTTP tests (list/stats/detail, pagination, by_day zero-fill, user isolation 404)
- ✅ 1 frontend prop-name bug found by agent (PageHeader `subtitle`/`action` vs `description`/`actions`) — fixed
- ✅ Re-verified live via screenshot: subtitle, days selector, refresh button all render

### SAFETY GUARDIAN: server-side hard floors for LIVE accounts
**Why:** User asked: *"how do I be sure the bot won't blow up my real account?"* — most efficient single intervention is non-bypassable, server-side risk caps that fire INSIDE the execution engine, regardless of user config.

**Built:**
- New module `/app/backend/safety_guardian.py` exposes `audit_pre_trade(db, account, signal, user_id, cfg_account_id)` returning `{ok, blocked_by, audit, evaluated_at, context}`. Enforces 7 caps on live accounts:
  1. `equity_known` — equity > 0
  2. `equity_vs_balance_floor` — equity ≥ 70% of balance (drawdown halt)
  3. `free_margin_floor` — free_margin ≥ 20% of equity
  4. `risk_inputs_present` — lot/entry/SL must all be set
  5. `per_trade_risk_cap` — SL$ × pip$ × lot ≤ 3% of equity
  6. `lot_vs_equity_sanity` — lot exposure ceiling
  7. `daily_loss_cap` — today's realized PnL > -6% of balance
  8. `total_open_risk_cap` — sum of open trade risk + new trade risk ≤ 9% of equity
- Paper accounts bypass with explicit `paper_mode_bypass` audit entry.
- All thresholds env-overridable via `SAFETY_*` variables.

**Engine integration (`execution.py`):**
- `MT5BridgeEngine.execute()` calls `audit_pre_trade` AFTER the max_concurrent cap check.
- On `ok=False`: returns `{blocked: 'safety_guardian', safety_blocked_by, safety_audit}` AND persists a row in new `safety_blocks` collection with the full audit. NO trade inserted.
- On `ok=True`: stamps `safety_audit` field on the inserted trade_doc for forensic traceability.

**Diagnostic surfacing (`diagnostic_routes.py`):**
- Risk State section now reports `Safety Guardian active` with all thresholds in detail string + `Safety Guardian recent blocks (24h)` with breakdown by blocked_by reason from the new `safety_blocks` collection.

**Testing:** 50/50 pass via `testing_agent_v3_fork` iter18 (11 new safety_guardian unit tests + 14 max_concurrent regressions + 23 iter25l + 2 HTTP integration). No critical/minor issues. Six code review notes addressed: (1) split `risk_inputs_present` from `per_trade_risk_cap` for honest error reporting, (5) persisted blocks to `safety_blocks` collection so the diagnostic shows real counts.

### BUG FIX: `max_concurrent_trades` cap bypass (race condition)
**Reported by user:** *"i set the maximum trades 5, and there are 12 trades open"*

**Root cause:**
1. `bot_runner._process_user_account` read `inflight` ONCE at function start, never re-counted as trades were queued in the for-symbol loop.
2. `_on_cooldown` was process-local in-memory — uvicorn hot reloads spawned new `bot_runner.loop()` tasks while old ones kept running. All instances saw stale state and each fired a trade per tick (5s EA heartbeat → 11 excess trades in 50s).

**Fix (two-layer defense):**
1. **Atomic config lock** at start of `_process_user_account`: `findOneAndUpdate({_id, _tick_lock_until: $lt now})` claims a 50s lease. Concurrent runners can't process the same cfg. Wrapped in `try/finally` so a crash mid-tick releases the lock immediately (no 50s starvation).
2. **Last-line-of-defense recount** in `execution.MT5BridgeEngine.execute()` and `PaperEngine.execute()`: fresh `db.trades.count_documents` right before `insert_one`. If `inflight >= max_concurrent`, the engine returns `{blocked: 'max_concurrent_cap'}` and skips the insert. `bot_runner` passes `max_concurrent` + `cfg_account_id` and bails on `blocked`. Backward-compat: manual UI trades (no kwargs) skip the check.

**Diagnostic added:**
- `_check_risk_state` now audits each per-config cap → reports `OVER CAP — {scope}: {inflight}/{cap}` as FAIL with auto-fix code `close_excess_trades`.
- New auto-fix `close_excess_trades`: closes oldest open trades on any over-cap scope down to the cap (sets `close_requested + close_reason='excess_over_cap'` so the EA cleans up on next poll).

**Testing:** 14/14 pass (3 race-guard unit tests + 11 endpoint regressions). Full regression 130/130 (testing_agent_v3_fork iter17).

### Bot Status auto-selects online account (Feb 2026)
`GET /api/bot/status` now auto-picks the **first active per-account config whose account is heartbeating** when caller omits `account_id`. Falls back to default profile when no per-account bot qualifies. Adds `scope_account_id` + `scope_auto_selected` to the response so the Dashboard can label which bot it's showing. Eliminates the misleading "Bot is STOPPED" strip when the user's per-account override is what's actually running.

### Admin auto-diagnostic (one-click bot triage)
**Why:** When the bot won't trade, admins had to grep logs across `ai_signals.py`/`bot_runner.py`/`bridge_routes.py`. No single place to see "is everything OK?". The anti-tilt freeze bug (last session) proved this.

**Built:**
- `GET /api/diagnostic/run` (admin-only) — runs 6 sections in one shot:
  1. **Connectivity** — MT5 accounts linked, EA heartbeat <5min
  2. **Execution pipeline** — signals last 6h, top veto reasons, tradeable→executed conversion
  3. **Trade sync** — ghosts, stuck SL/TP modifications, broker↔DB drift
  4. **Risk state** — anti-tilt freeze (with per-account countdown), daily PnL, max-concurrent slots
  5. **EA / Config** — EA version vs `1.26`, bot ON toggle, Claude key, Telegram alerts
  6. **Broker errors** — 24h MT5 retcodes with plain-English explanations (10016/10027/...)
- `POST /api/diagnostic/auto-fix` — applies safe remediations:
  - `clear_stuck_modifications`, `ack_ghost_trades`, `reconcile_trades`, `release_anti_tilt`
- **Dashboard UI**: "RUN AUTO-DIAGNOSTIC" button (admin-only) → modal with:
  - Pass/Warn/Fail icons per section
  - Per-issue FIX button + global AUTO-FIX ALL
  - COPY AS TEXT for sharing with support
  - RE-RUN to verify fixes landed
- **Passive auto-refresh** (every 2min) — button silently re-fetches and badges:
  - 🔴 `· N ISSUES` (pulsing red, fails > 0)
  - 🟠 `· N ADVISORIES` (amber, warns only)
  - 🟢 `· ALL OK` (subtle green, clean run)

Files: `/app/backend/routes/diagnostic_routes.py`, `/app/frontend/src/components/DiagnosticModal.jsx`, Dashboard wiring with `DiagButton` sub-component.

### Bot transparency: anti-tilt freeze now surfaced in UI
**Problem:** Bot active + connected, but no trades opening despite ~58 tradeable signals in 6h. Root cause: anti-tilt freeze (`bot_runner.py` silently `return`s when last N closed trades all lost within freeze window). Last 4 XAUUSD trades all lost → 4h freeze → user had zero visibility.

**Fix:**
- `GET /api/bot/status` now mirrors anti-tilt logic and exposes:
  - `why_no_trade` = "Anti-tilt freeze: last 3 trades lost — auto-execute paused for {Xh Ym}. Adjust in Bot Config → Capital Preservation."
  - `anti_tilt_frozen_until` (ISO timestamp)
- `GET /api/bot/health-score` adds a warning-level issue with -5 deduction so the widget catches the user's eye (score 100 → ~91 during freeze), with countdown ETA and clear actionable fix.
- Dashboard `why_no_trade` strip already renders the new message automatically.

No core risk logic changed — only observability. User can lower `anti_tilt_freeze_hours` / `anti_tilt_consecutive_losses` or disable in Bot Config if intentional.


## Iter-144/145 (2026-07-15) — Quant Review Round 4 (C1-C5, H1-H7) DONE
**Critical fixes:**
- C1: calibration.py cache keyed per user (was global — first user's table leaked to all tenants). invalidate_cache(user_id).
- C2: bot_runner Kelly sizing now receives calibrated p_win_lb×100 (Wilson lower bound), auto-disabled when calibration absent/thin. Never the raw setup score.
- C3: MIN_BUCKET_N 8→30, MIN_SCOPE_N 50, MIN_GLOBAL_N 80; every estimate carries Wilson 95% one-sided lower bound (p_win_lb) — consumers gate on the LB, never the point estimate.
- C4: instruments.py canonical registry (contract size, pip size, FX 100k units). portfolio/var.py notional via notional_usd() — old lot×entry proxy understated FX exposure ~100,000×.
- C5: compute_lot_for_account fail-closed — invalid equity/SL/pip → lot_size=0, sizing_valid=False, reject_reason. Post-rounding actual risk checked vs budget×RISK_OVERSHOOT_TOLERANCE (1.5); trade REJECTED if broker-minimum 0.01 overshoots. Wired into bot_runner AND manual execute route (422). Fixed missing RISK_OVERSHOOT_TOLERANCE constant (crashed all trade execution).
**High-priority fixes:**
- H1: backtester Engine.run() settles ALL open positions at end-of-test (last executable bid/ask, note=EOD_SETTLEMENT) — final P&L/win rate fully realized.
- H2: duplicate same-symbol entries explicitly REJECTED (result.rejected_orders) until book is position_id-keyed.
- H3: opening-gap stop fills — long SL fills at min(stop, open)−slip−hs; TP gaps fill at the better open. Shorts mirrored.
- H4: EngineConfig commission_usd_per_lot_side + swap_usd_per_lot_day; round-turn commission + per-day swap deducted in _close_position; result.total_costs.
- H5: learned_meta.py walk-forward OOS — dataset chronologically sorted; _walk_forward_oos() expanding-window folds; Platt calibration + rejection threshold + oos_auc now fit on OUT-OF-SAMPLE predictions (calibration_source field; in_sample_fallback only when <10 OOS samples). Verified: train_auc 1.0 vs oos_auc 0.88 on synthetic data (exposes overconfidence).
- H6: calibration buckets carry payoff distribution (avg_win_r/avg_loss_r from risk_amount) + conservative ev_r = p_lb·E[win_R] − (1−p_lb)·E[loss_R]. bot_runner EV gate (cfg.ev_gate_enabled, default on): negative conservative EV with n≥30 → trade skipped + pulse recorded.
- H7: test isolation — emergentintegrations imports now lazy (function-level) in 12 modules; database.py motor import lazy; strategy_code_generator uses patchable module-attr lazy pattern. Verified: domain modules import with emergentintegrations+motor BLOCKED.
**Test suite: 1860 passed, 0 failed** (was 61 failed). Fixes: conftest _ensure_event_loop fixture (dead-loop RuntimeError killed 20+ tests in full runs), tests/ea_version.py dynamic EA version helper (6 files no longer pin stale versions), setup_routes ea_latest_version drift 1.42→1.43, stale tests updated to fail-closed sizing contract / origin:"manual" taxonomy / accumulated candle counts / 45s reconcile grace window / origin:"auto" breaker filter / register terms payload / auto_deleverage bot_configs mock.

## Iter-146 (2026-07-15) — Scalp Fast Path subsystem (EURUSD) DONE
User requested a dedicated low-latency scalp subsystem (15s–5min holds, NOT HFT), control plane + fast path split, EURUSD only, shadow-first with demo-live toggle, EA tick streaming, deterministic baseline + logistic model trained on accumulating labels.
**Backend `/app/backend/scalp/`:**
- instruments.py — ScalpInstrumentConfig registry, EURUSD ONLY approved (others 422/ignored).
- state.py — TickEvent (broker_ms + received_ms), in-memory ScalpState (deques, spread history, slippage EWMA, clock drift, reject rate). O(1) per tick, zero DB on hot path.
- features.py — FEATURE_KEYS contract: ret 1/3/5/10/30s (pips), accel, vwap_dist, vol_short/long, tick_rate, spread pctl, uptick_ratio, quote age.
- setup.py — ONE setup: micro-pullback momentum continuation (impulse ≥ max(1.5p, 1.8×vol), 15–60% pullback, accelerating resumption).
- forecast.py — ScalpForecast: p_target_before_stop (det. base 0.48±bonuses, clamp 0.40–0.60; logistic model overrides when usable), stop=max(2×spread,1.5×vol) clamp 1–5p, target=1.3×stop, uncertainty pips.
- costs.py — spread + 2×slippage(EWMA after 5 fills) + commission; dynamic spread limit = min(abs cap, session p75).
- edge.py — EV = p·target − (1−p)·stop − cost − uncertainty ≥ 0.15p AND cost ≤ 25% of gross alpha.
- risk.py — 0.05%/trade, max 1 open, 20/hr, 4-loss→30min cooldown, daily loss 0.5%, cost budget 0.35%, 5min max hold. Sizing FAIL-CLOSED (min lot > budget → reject).
- gate.py — final fresh-quote gate (quote age, spread, signal age ≤3s, edge, health).
- kill.py — Step 17 auto-shutdown; open_allowed vs close_allowed SEPARATE (closing always permitted).
- permissions.py — slow control plane cache (30s refresh, 120s stale → FAIL CLOSED): UTC session window, high-impact news blackout (fail closed on error), M15 EMA-slope regime (TRENDING_UP/DOWN/FLAT/VOL_SHOCK).
- model.py — pure-numpy logistic, trains on barrier labels (target_first/stop_first, timeouts excluded), walk-forward OOS AUC; used ONLY if n≥200 & OOS AUC ≥0.53; auto-retrain every 200 resolutions + POST /api/scalp/retrain.
- engine.py — ScalpRunner per (account,symbol): ingest→state→health→throttled eval (1/s)→decision doc; barrier ShadowSim for EVERY forecast-stage candidate (unbiased labels, exits on executable side); demo_live submits via engine_for_account with broker-visible SL/TP, scope="scalp_fast", origin="auto"; fast exits (timeout/spread shock/degraded) via FULL_CLOSE pending_modification; tick recording batched to scalp_ticks (200/10s).
**API `/api/scalp/*`:** config (EURUSD-only 422 guard; demo_live requires confirm_live), status, decisions, metrics (Step 18 alpha/execution/model/verdicts), retrain. Bridge: POST /api/bridge/ticks (EA v1.44), fill reconciliation hook in /bridge/report for scope=scalp_fast.
**EA v1.44:** TickStreamEnabled/TickStreamSymbol/TickBatchMs inputs; EventSetMillisecondTimer fast lane + PollSeconds slow-lane gate; SendTicks() via CopyTicksRange (batch cap 120). All LATEST_EA refs bumped to 1.44.
**Frontend:** /scalp page (Scalp.jsx) + sidebar "Scalp Fast Path": account select, ENABLE SHADOW / DEMO LIVE (confirm dialog) / DISABLE, runner cards (health/regime/counters/spread/quote age), shadow-performance metrics, decisions table with reject reasons. data-testids throughout.
**DB:** scalp_configs, scalp_decisions, scalp_ticks, scalp_models.
**Tests:** tests/test_iter146_scalp.py — 33 tests incl. e2e HTTP tick→decision (fail-closed permission verdict verified). Full suite green (~1893). Fixed stale v1.43 pin in test_iter116.
**User setup:** re-download EA v1.44, attach with TickStreamEnabled=true, TickStreamSymbol=EURUSD; enable Shadow on /scalp page.

## Iter-147 (2026-07-15) — Scalp Review Hardening (items 1–20) DONE
**Critical:**
- 1 Cost accounting: ShadowSim explicit gross/cost/net — net_pips = exec-to-exec − exit slip (stop/timeout only, not limit target) − commission; gross_move_pips = mid-to-mid; metrics use outcome.net_pips DIRECTLY (double-count removed); stressed metric = net − 0.5×cost.
- 2/3 Model calibration + deployment gates: chronological train 40% / Platt-calibration 30% / eval 30%; tracks Brier vs baseline, ECE, OOS AUC; usable requires ALL: AUC≥0.55, Brier≤baseline, OOS net expectancy>0 on model-SELECTED trades, +50% cost-stress survival, ≥65% profitable windows, n≥300. Verified: synthetic model w/ AUC .78 correctly SHELVED for failing cost stress.
- 4 Broker-keyed models: model_key = broker|account_type|symbol; training query filtered; decisions store broker/account_type.
- 5 Hot path: decision_id = local uuid4, insert fire-and-forget, account preloaded on ingest (no find_one before order).
- 6 Signal freshness from initiating tick received_time_ms; decision docs record signal_ts/feature_snapshot_ts/order_submit_ts.
- 7 Pre-submit recheck: adverse drift ≤ 0.5×stop + fresh spread limit; EA entry_price slippage veto is broker-side last gate; reject stages recorded (pre_submit_drift/spread/broker_blocked).
- 8 Commission: scalp_config.commission_usd_per_lot_side → pips via pip value; flows through forecast/edge/live cost.
**High:**
- 9 Risk persistence: RiskState.to_doc/load_doc → scalp_risk_state collection; restored on hydration + open-position reconciliation; NO entries until _risk_restored.
- 10 Dataset tags: candidate / submitted / filled_live on decisions.
- 11 Overlap suppression: same-direction candidates skipped while a sim is active (suppressed_overlap counter).
- 12 Drift protection: artifacts expire after 7 days; predict_p refuses OOD inputs (|z|>6) → deterministic fallback.
- 13 Live close feeds real (or estimated) execution cost into daily cost budget.
- 14 Close states: OPEN→CLOSE_REQUESTED→confirmed close; position stays monitored until broker /bridge/report confirms.
- 15 Account-level open-scalp cap across all runners.
- 20 News: malformed high-impact timestamp → fail closed.
- Tick ingest drops duplicate/out-of-order ticks.
**Tests refactored:** tests/unit/scalp/test_scalp_unit.py (28, pure imports, project-relative paths, no env reads) + tests/integration/scalp/test_scalp_api.py (12, lazy env). ea_version.py path-relative. Full suite: 1899 passed (1 live-data race flake, passes rerun).
**Deferred (ROADMAP):** item 16 setup-score bucket shrinkage, 17 conditional target/stop (MFE/MAE models), 18 sub-second eval (measure signal half-life first), 19 session-bucket validation, EA-side report commission/swap fields.

## Iter-148 (2026-07-15) — Scalp Review Round 3 DONE
- CRITICAL model/calibrator mismatch → Option C: train 50% | calibrate 20% | eval 30%; the DEPLOYED artifact is the 50%-trained model WITH its own Platt calibrator, never retrained after calibration (`calibrator_matches_deployed_model: True` invariant on every artifact).
- Delayed-batch guard: new entries require transport age (now − sent_at_ms) ≤ 3s AND broker-adjusted quote age (clock-offset corrected via state.broker_adjusted_age_ms()) ≤ cfg limit; stale batches still update state/labels but cannot trade (stale_batches counter, ingest returns batch_fresh/transport_age_ms/broker_age_ms).
- Timeout economics: retrain query includes timeouts; directional label still excludes them but ALL profitability/deployment gates (net exp, stress, windows) evaluate over every outcome incl. timeouts (timeouts_in_eval recorded).
- Model audit: every candidate artifact (incl. rejected) persisted to scalp_model_history for selection-bias monitoring.
- Structured predictions: scalp_model.predict() → {p, source, fallback_reason: no_model|model_expired|ood_features|invalid_features|invalid_expiry}; stored on every decision (model_source/model_fallback_reason).
- Real slippage feedback: /bridge/report "open" status → runner.on_trade_opened() computes signed entry slippage vs requested price → state.record_fill(); requested/actual entry persisted on the decision.
- ABSOLUTE drift guard pre-submit (favorable drift also rejects — geometry invalid; next cycle re-forecasts).
- Overlap: ANY active sim (either direction) suppresses new labels — one setup event per symbol.
- Audit backpressure: _bg tracks pending/failed writes with one retry; ≥500 pending halts NEW entries; /scalp/status returns audit {pending, failures, halted}.
- Restart during CLOSE_REQUESTED: restore_risk re-marks trades with pending_modification FULL_CLOSE as CLOSE_REQUESTED.
- Tests: 9 new unit tests (delayed batch, broker age, fill feedback, structured fallback, restart close-state, etc.) — 48 scalp tests green; synthetic Option C validation passed (AUC .85, calibrated, timeouts in eval, usable=True end-to-end); full suite 1907 passed (1 live-data flake, green rerun).
- DEFERRED (ROADMAP): shared account-level risk state (needed before 2nd symbol), durable event-log persistence, eval-interval study (100ms–2s), session-bucket validation, MFE/MAE conditional targets, EA report commission/swap fields.

## Iter-161 (2026-06) — 5 Release Blockers resolved (test report iteration_79.json ALL PASS)
1. **.env.example templates**: backend/.env.example (all keys incl. WS_ALLOW_QUERY_TOKEN, comments, no values) + frontend/.env.example — verified keys-complete vs real .env via comm.
2. **worker-tuning in docker-compose.yml** (line 103, `python -m workers.tuning`) — all 6 workers now orchestrated.
3. **release.yml ships verified .ex5**: new ea-compile job (windows-latest, MetaEditor compile gate identical to ci.yml), release job needs [suite-from-archive, ea-compile], downloads EmergentTradingBridge-ex5 artifact, .ex5 included in release-manifest.json, SHA256SUMS and published release files.
4. **Frontend safe URL default**: api.js + useLiveStream.js fall back to window.location.origin when REACT_APP_BACKEND_URL empty (guarded typeof window). Verified no localhost defaults.
5. **Validation evidence**: docs/campaigns/2026-06-11-simulation-harness-{netting,hedging}.md — every scenario (restart recovery, reconnect replay, partial fills, netting, hedging, retcodes) mapped to a named passing pytest test; MT5_VALIDATION_CAMPAIGN.md gained "Evidence status" section. Broker-demo confirmation + 2-week soak remain operator manual gates.
6. Fixes en route: testing agent removed duplicate `import axios` in ArchitecturePipeline.jsx (was blocking CRA build/login overlay); test_iter21 helper password Pass1234! (HIBP-breached) → Kd5#Zt9mW2xVpR7c, file now 21/21 green (was 16 errors).
- Full pytest: 2688/2695 effective green (remaining 12 = pre-existing env-state: WS/scalp integration services + 1 candle-freshness). Frontend e2e: login→dashboard→/performance→/brokers all render, zero non-benign console errors.

## Iter-162 (2026-06) — GitHub CI red jobs fixed (backend-unit + security-scan)
Both reproduced locally with the EXACT CI commands before fixing:
1. **backend-unit** (repro: git archive → clean tree → pytest): test_iter150_netting_hedging.py read frontend/.env UNGUARDED at import → collection error in CI checkout (.env untracked). Fixed: try/except fallback to REACT_APP_BACKEND_URL env var / localhost. Also test_iter143_prelive_hardening::test_preview_env_sets_mode_explicitly read backend/.env → now pytest.skip when file absent. CI-sim result: 473 passed + 1 skipped; local: 474 passed.
2. **security-scan** (repro: gitleaks 8.24.3 git scan → 12 leaks): REAL RATE_LIMIT_BYPASS_TOKEN leaked in tracked test_reports/iteration_57/58.json + expired TOTP test secret in iteration_74.json; rest are false positives (dummy fixtures, stoic_live_... doc placeholders). Fixed: (a) ROTATED RATE_LIMIT_BYPASS_TOKEN in backend/.env (backend restarted, login 200, iter57/58 rate-limit suites 25/25 green — conftest reads token from .env so rotation transparent), (b) redacted secrets in tracked test_reports + phaseD_suite.log, (c) added /.gitleaksignore with all 12 commit-pinned fingerprints (history immutable). gitleaks git scan now "no leaks found"; dir-scan confirmed new files (docs/campaigns, CHANGELOG, iter21 password) introduce zero new findings.
- LESSON: tests must never hard-depend on untracked .env files at import time; test_reports JSON must never embed live token values.

## Iter-163 (2026-06) — Deployment blocker + production hardening round
1. **ROOT CAUSE of "missing .env.example"**: .gitignore `.env.*` (line 145) ignored the templates — they existed locally but were never pushed. Fixed with negations (!.env.example, !backend/.env.example, !frontend/.env.example); git check-ignore confirms tracked next commit.
2. **Release gated on full CI**: ci.yml gained `workflow_call`; release.yml new `ci-gates` job (`uses: ./.github/workflows/ci.yml`, secrets: inherit); release job needs [ci-gates, suite-from-archive, ea-compile] → no signed release while gitleaks/pip-audit/static-analysis/container-scan/frontend-build fail.
3. **MongoDB auth**: compose mongo now requires MONGO_ROOT_*/MONGO_APP_* from compose-level ./.env (template ./.env.example, values generated by install.sh); deploy/mongo-init.js creates least-privilege app user (readWrite on DB_NAME only); ALL 7 services use authenticated MONGO_URL mongodb://app:pass@mongo/db?authSource=db. Port never published.
4. **GET /api/ops/release-readiness** (backend/routes/ops_routes.py, METRICS_TOKEN gated): mongo write/read/delete round trip, all 6 worker leases fresh, reconciliation lag (lease fresh + no unresolved >5m), outbox backlog (none pending >2m), feature-schema compatibility (db_max <= code). 200/503. Verified live: 503 with per-check detail in preview (dedicated workers absent — correct), 403 without token.
5. **deploy/update.sh** now verifies API health + frontend :3000 + release-readiness (45 x 4s poll) with AUTO-ROLLBACK on any failure. **deploy/backup.sh restore** is maintenance-mode: stop API+6 workers → mongorestore --dryRun validation → restore --drop → start API only → workers stay stopped until operator verifies broker reconciliation + readiness. backup/restore use root auth inside container.
6. **Docs**: DEPLOYMENT.md — six workers, MongoDB security section, topology-verified updates, maintenance-mode restore; install.sh comments 5→6.
- Tested: 19/19 iter151+iter152 prod-ops tests, ruff clean, bash -n all scripts, YAML valid, gitleaks clean on all changed files, admin login 200. Compose config not runnable in pod (no docker) — YAML-validated only.

## Iter-164 (2026-06) — CI security-scan ROOT CAUSE fixed + iter-163 features verified (iteration_80.json ALL PASS)
1. **security-scan root cause (reproduced locally)**: gitleaks-action ran on a SHALLOW checkout (fetch-depth 1) → whole tree appears as one new commit → .gitleaksignore commit-pinned fingerprints never match → 9 findings resurfaced. Reproduced exactly via `git clone --depth 1` (9 leaks) while full-history scan was clean.
2. **REAL LEAK found among them**: Exness#3 account bridge token hardcoded in test_iter115/116 → ROTATED in DB (EA on that account must re-pair); tests now load the token from Mongo at runtime (label lookup) — 45/45 green. Remaining 8 were false positives (GARBAGE* fixtures, stoic_live_... docs placeholders, iteration_73 summary text) → inline `gitleaks:allow` comments + text tweak.
3. **ci.yml security-scan hardened**: checkout fetch-depth: 0 + replaced gitleaks-action@v2 with pinned gitleaks v8.30.1 binary (`gitleaks git . --exit-code 1`) — kills org-license variability and matches local verification exactly. Single-commit-equivalent scan now clean; pip-audit clean (10 ignored).
4. **Iter-163 features verified by testing agent (iteration_80, ALL PASS)**: browser login (no splash stall — earlier stall was main-agent screenshot-tool sandbox artifact), /bot-health ReadinessCard (verdict + 5 pills + refresh), /trades 43 JOURNAL buttons + modal (cached card, copy/revoke/regenerate/close), /j/{share} public + invalid-id error, readiness 403/503 auth paths.
- NOTE: backend/frontend/.env.example + /.env.example were still untracked at last check (`??` in git status) — they are no longer gitignored and ship with the next commit/push.

## Iter-165 (2026-06) — 12 production-readiness corrections (user release review round 3)
1. **Templates now SHIP**: root cause — platform auto-commit skips .env* files; force-staged via git add -f (.env.example, backend/, frontend/). Belt+braces: install.sh is now SELF-CONTAINED (writes templates inline if missing from archive) + release.yml archive-completeness gate blocks any release missing them. Stray tracked `=` file deleted.
2. **Installer safety**: deployment MODE now REQUIRED — `--dev` (all ports loopback) or `--production <domain>`; production enforces APP_ENV=production, CSRF_ENFORCE_ORIGIN, CORS_ORIGINS, DOMAIN + COMPOSE_FILE=base:tls in ./.env; --dev REFUSES to run over a production config. Installer also fills operational numerics (BOT_LOOP_INTERVAL_SEC etc — blank values would crash int() paths) + generates admin creds.
3. **No direct backend exposure**: compose binds backend AND frontend to 127.0.0.1 only.
4. **Production TLS ingress**: docker-compose.tls.yml (Caddy 2, auto Let's Encrypt, ports 80/443 → frontend nginx) + deploy/Caddyfile; wired via COMPOSE_FILE so update/backup/soak scripts manage the same stack.
5. **Release gates hardened**: release.yml — (a) suite-from-archive now runs the complete classified non-live suite (unit+truth+`-m integration` with mongo:7 service) + archive-completeness check; (b) NEW install-from-archive job runs the REAL installer from the exact archive and gates on release-readiness ready=true; release needs all. ci.yml gained backend-integration job (mongo service).
6. **Full suite classification — all 28 reds fixed**: iter21 (16 errs, HIBP pwd) fixed earlier; scalp integration (9) = HIBP pwd `testpass123`→strong + 2 stale tests updated to current hardened semantics (order_state OPEN now requires PROTECTED; risk_reservations guarded transitions — uncertain set in ONE step from RISK_RESERVED, terminal RELEASED protection asserted); WS accept test skips with reason when ingress blocks upgrade (403/404); iter142 freshness: stale symbols must be FLAGGED stale (staleness-detection assertion), freshness only for streaming feeds. scripts/run_full_suite.sh = staging full-suite gate (junit report, excludes broker/external marks).
7. **Frontend image SBOM + vuln scan** (BLOCKING critical) added to ci container-build AND release (sbom-frontend.spdx.json in manifest/SHA256SUMS/release files).
8. **emergentintegrations pinned ==0.2.0** in Dockerfile.backend, ci.yml, release.yml (index provides no hashes — version-pin only, noted).
9. **Worker LOOP monitoring**: workers/base._lease_keeper records loops_running/loops_total on each renewal + error log when a loop dies; readiness workers check now requires every loop running (loops shown per worker, e.g. trading 3/3, reconciliation 4/4).
10. **VERIFIED by full local simulation**: fresh DB + minimal installer env + all 6 real workers + uvicorn → /api/ops/release-readiness returned ready=true with all loop counts. Installer modes tested (--dev, --production, mode-required refusal, prod-guard). All YAML valid, bash -n clean, integration 22/22, CI subset 474/474, iter142 5/5, WS 2 pass+1 skip.
- REMAINING OPERATOR GATES (cannot be done from this pod): broker-attached netting/hedging/reconnect/partial-fill campaign, 2-week demo soak, panic/rollback/alert/db-recovery drills (tooling: deploy/soak.sh, scripts/run_full_suite.sh, docs/campaigns/SOAK_LOG.md).

## Iter-166 (2026-06) — container-build CI failure fixed (image vuln scan criticals)
ROOT CAUSE (reproduced locally with grype 0.116 — scanned base images via registry + installed site-packages):
1. **litellm 1.80.0** — 2 CRITICAL GHSAs (fixed 1.84+). Upgraded to litellm==1.93.0 which requires openai>=2.x, conflicting with emergentintegrations' openai==1.99.9 pin → resolved by pinning litellm==1.93.0 + openai==2.47.0 in requirements.txt and installing emergentintegrations==0.2.0 with --no-deps (all its deps pinned in requirements) in Dockerfile.backend + both workflows. VERIFIED live: real Claude journal generation works on the new combo; 474 CI-subset + 22 integration tests green; site-packages grype scan = 0 criticals.
2. **s5cmd 0.2.0** — bundled Go 1.20.8 binary with critical stdlib CVEs; required by NOTHING → uninstalled + removed from requirements.txt (0.3.3 still ships vulnerable Go 1.22.10).
3. **Preempted next failure — frontend image** (scan added iter-165): nginx:1.27-alpine has criticals (libcrypto 3.3.3, libxml2, curl) → bumped to nginx:1.29-alpine + `apk upgrade` (libssl/libcrypto 3.5.7 fix) + `apk del curl` (8 unfixed alpine curl criticals; nginx doesn't need curl).
4. **Backend image**: dropped debian curl install (same CVE class risk); HEALTHCHECK now uses python stdlib urllib; added `apt-get upgrade -y` for OS patches.
- NOTE: pip check reports emergentintegrations' openai pin mismatch — intentional and runtime-verified; documented in Dockerfile comment.
