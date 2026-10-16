# STOIC v1.60.55 — review fixes for PR #102–#105 (v1.60.52–v1.60.54)

Protected paths untouched. Unit tests for every item (`tests/unit/test_v1_60_55_review_items.py`,
`tests/integration/test_v1_60_54_close_watchdog.py` extended).

## #44 — close watchdog: mandatory closes never expire
* Reason classes (`close_watchdog.is_mandatory_close`): **mandatory** = panic, friday_flat, eod_flatten, pre_news_protect,
  emergency_unprotected / emergency_specs_stale (protection), manual, manual_telegram, nl_command, excess_over_cap;
  **discretionary** = auto_deleverage_* (var_cap, sector_cap, hard_drawdown, combined_corr), time_stop, take_profit, scalp exits.
* Mandatory: never expired. While unconfirmed, the watchdog RE-DISPATCHES a fresh fenced FULL_CLOSE with exponential
  backoff (60 s → 120 → 240 … capped 15 min, `CLOSE_REDISPATCH_BASE_SEC=60`, `CLOSE_REDISPATCH_MAX_SEC=900`) until the
  terminal confirms or the position is gone. An offline terminal finds the close waiting when it returns.
* Discretionary: expire after `CLOSE_REQUEST_TTL_SEC` (300) **only while the EA is online** (heartbeat ≤
  `CLOSE_WATCHDOG_EA_ONLINE_SEC`=90 s); offline → kept pending (`kept_offline_at`). The 60 s `close_command_unacked`
  alert is unchanged for both classes.

## #45 — sector-cap / correlated-bucket deleverage is ADVISORY (new flag, default OFF)
* v1.60.54's instrument-table notional (100,000 units/lot forex, ×100 for every gold alias) made the fx_major (600 %),
  fx_minor (300 %), commodity (500 %) caps and the combined-corr bucket reachable for the first time. That would have
  been a new auto-close behaviour. Now: `SECTOR_CAP_DELEVERAGE_ENABLED` (env, **default false**) — when off, breaches
  produce `snapshot.advisory_actions` / `advisory_triggers`, an ops alert `sector_cap_advisory` (medium, dedup per
  account+trigger) and a bot pulse, **no close**. When on, the cull runs as before.
* Sector-cap / corr culls honour the SAME exemptions as var_cap: zero-open-risk (BE) positions are never candidates and a
  book whose stop-bounded loss (×1.5) is inside the VaR cap is never culled.

## #46 — one break-even default, breakeven_enabled honoured, cost buffer, migration report
* `models.BREAKEVEN_TRIGGER_R_DEFAULT = 1.0` is the ONE default: `effective_breakeven_trigger_r(cfg)` is what
  trade_manager reads (missing value → 1.0, never silently 0; `breakeven_enabled=False` → disabled; 0 → disabled).
  BotConfig.jsx mirrors the constant and exposes the `breakeven_enabled` toggle. `breakeven_enabled` is back in the model
  (default true) and out of the dead-field lists.
* Cost buffer: the BE stop sits on the profitable side of entry by spread + commission (`scalp_breakeven_sl`, same
  helper the scalp engine uses) and is not armed until the trade is past costs — a BE fill nets ≥ 0, never a small loss.
* `scripts/report_breakeven_migration.py` — DRY RUN listing every bot config's effective trigger BEFORE (old runtime:
  raw stored value or disabled) and AFTER. Run it on production before deploying; configs without a stored value go
  from "disabled" to 1.0R — intended, and the report makes it explicit. Nothing is written.

## #47 — small fixes
* Scalp fast path reserves `user_direction_cap` with `base_symbol(self.symbol)` (GOLD# → XAUUSD).
* `trade_manager._skip_pulse_at` is pruned (entries > 1 h old, once it exceeds 500 keys).
* `broker_price.from_report`: a `live_price` paired only with legacy `live_at` counts as fresh only for EA ≥ 1.66;
  otherwise `live_price_at` (the v1.60.53 receive stamp) is required. trade_manager passes the account's `ea_version`.

## #48 — mandatory closers take over unacked closes; friday_flat re-issues
* `portfolio/risk_manager.execute_deleveraging_actions` sends a fenced `FULL_CLOSE` (confirmed, v1.60.54).
* `close_commands.close_in_flight(trade)` is True only when the terminal has ACKED the outstanding close.
  `close_commands.mandatory_close()` issues a mandatory close that takes over any unacked request (new fenced command
  supersedes it) and re-issues its own with the #44 backoff until the position is confirmed gone.
* friday_flat: no more `close_requested` / `friday_flat_at` exclusion — a VaR-flagged position is no longer skipped
  (it stayed open over the weekend); closes go through `mandatory_close` (re-issue until gone); tighten stays one-shot.
* eod_flatten: same takeover + re-issue (no `pending_modification` skip).
* panic: supersedes every outstanding close and, on EAs ≥ 1.40, rides the fenced FULL_CLOSE path (explicit acks);
  legacy EAs keep the trades-block flag.

## Finnhub — explicit statement of trading effect (requested)
With `FINNHUB_API_KEY` set, Finnhub becomes the PRIMARY economic-calendar source for `economic_calendar.get_events()`,
and therefore for `macro_freeze_check` — a REAL entry gate (macro freeze windows, avalanche guard, pre-news protection
derive from these events; ForexFactory / financecalendar remain fallbacks). Finnhub forex/general headlines also feed
`news_understanding` scoring, which drives the news veto, confidence adjustment and size trim. Both are intended;
without the key the behaviour is unchanged. The GDELT / central-bank / GVZ-VIX risk regime stays shadow-only.

## New env / config in v1.60.55
| name | default | effect |
|---|---|---|
| `SECTOR_CAP_DELEVERAGE_ENABLED` | `false` | sector-cap / combined-corr deleverage closes (off = advisory alert + pulse) |
| `CLOSE_WATCHDOG_EA_ONLINE_SEC` | `90` | heartbeat age that counts as "EA online" for discretionary close expiry |
| `CLOSE_REDISPATCH_BASE_SEC` / `CLOSE_REDISPATCH_MAX_SEC` | `60` / `900` | mandatory close re-dispatch backoff |
| Bot Config `breakeven_enabled` | `true` | master switch for the non-scalp break-even rule |
| Bot Config `breakeven_trigger_r` | `1.0` (shared `BREAKEVEN_TRIGGER_R_DEFAULT`) | break-even trigger in R (0 = TP1-only) |
