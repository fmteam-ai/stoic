# STOIC v1.60.54 — anti-tilt single source of truth · R-multiple contract sizes · Market Intelligence card

Scope: items #39, #40, #41. No trading-behaviour change except the corrected loss classification in #40
(fewer FALSE losses on broker-suffixed gold symbols). Protected paths untouched. No new env vars.

## #39 — Bot Health anti-tilt banner uses the SAME rule as bot_runner
* New `backend/anti_tilt.py` — `evaluate(db, user_id, cfg, now)` is the ONE evaluation: `origin=auto`,
  `stats_excluded != True`, loss = `streak_losses.is_streak_loss` (gave back > 0.1R of planned risk; EA-confirmed
  breakeven scratches and pnl ≥ 0 never count), newest N all losses within `anti_tilt_freeze_hours`.
* Callers: `bot_runner` (gate), `GET /bot/cooldowns` (`anti_tilt_trades[]` with `counts_as_loss` + `why`,
  `anti_tilt_rule`), `GET /bot/health-score` (issue `anti_tilt_freeze` lists the counted trades and why),
  `GET /bot/pulse`, diagnostics `_check_risk_state`. The old `all(pnl<0)` over any-origin trades is gone
  (16 Oct: "freeze 46m" on Bot Health while the runner had 0 blocks).
* UI: CooldownPanel frozen banner lists each judged trade (struck through when it does not count) + the rule.

## #40 — `analytics._r_multiple` contract size
* Symbol normalised with `base_symbol()` first (GOLD#, XAUUSD+, XAUUSD.crp → XAUUSD).
* Contract size from `instruments.known_contract_size(symbol, account)`: the account's EA-reported
  `symbol_specs[base].contract_size` first, then the instrument table (XAU 100, XAG 5000, forex 100,000,
  indices/crypto 1). Unknown → `None` (never the old default 1 that produced −10R clamps).
* `analytics.trade_r_multiple(trade, account)` uses `original_stop_loss` / `original_lot_size` when present;
  `streak_losses.classify_loss()` explains each verdict; `compute_sessions` passes per-account specs.
* `scripts/recompute_r_multiples.py` — dry run by default, `--apply` writes `trade.r_multiple`, `r_source`,
  `r_recomputed_at` on closed trades (never pnl). Preview dry run: 1607 closed trades, 331 R values change
  (XAUUSD.fx 113, XAUUSD-ECN 108, GOLD# 91, XAUUSD 12, GOLD 6, EURUSD# 1); e.g. XAUUSD.fx −17.08 was −10R → −1.02R.

## #41 — Market Intelligence card (display only)
* `GET /api/market-intel` (auth, user-scoped, read-only, credential-like keys scrubbed) — `backend/market_intel.py`.
  Sections: next_events (+freeze line, calendar source/refresh), headlines (last 4 h, direction per instrument,
  bias gauges), central_banks (latest per bank, last-60-min highlight), macro (DXY / DFII10 / GVZ / VIX with
  staleness → "old data"), geo_risk (7-day sparkline, top regions), risk_regime (`SHADOW — not affecting trades`
  while `geo_risk_gate_enabled` is off; a flagged-on config shows "FLAG ON — no gate behaviour is implemented yet"),
  usage per EURUSD/XAUUSD (macro freeze, avalanche guard, news bias + gate, gold off-hours / Asia skip, last
  decision citing these), source_health (Finnhub calendar/news, NewsAPI, RSS+AI, central-bank RSS, GDELT, FRED,
  DXY; `not_configured` is grey info).
* Storage added where missing: `news_ai_cache[base].items` (scored headlines with url/score/why); RSS/NewsAPI
  headlines now carry `url`.
* `economic_calendar.get_events`: a fresh process whose every fetch fails reuses the shared cache past its TTL
  instead of serving an empty calendar for 10 min.
* UI: `MarketIntelCard` (full) at the top of Agents, `compact` on the Dashboard; refresh 60 s; mobile layout.

## Tests
`tests/unit/test_v1_60_54_anti_tilt_r_multiple_market_intel.py` (19). Updated: `test_iter45_ghost_close_recovery`,
`test_v1_60_48_audit_p1` (source-grep assertions → shared helper).

## #42 — RoboForex XAUUSD pip value ×1000 off (HIGH)
* Root cause in this tree: `compute_lot_for_account` derived the pip value ONLY from the account's `account_type`
  label (`ACCOUNT_TYPE_LOT_MULTIPLIER`: microcent = ×0.001 → $0.01/pip for XAUUSD); the EA-reported symbol spec
  was never consulted. A RoboForex account carrying a cent/microcent label (or a mis-set type) sized 25 lots raw and
  only the 0.25 % risk clamp saved it.
* (a) `risk.broker_pip_value_usd_per_lot(symbol, account)` = tick_value × pip_size / tick_size from the EA spec
  (keyed by base symbol — GOLD#, XAUUSD+, XAUUSD.crp resolve to XAUUSD). `risk.resolve_pip_value()` prefers it,
  then the account_type table.
* (b) Sanity guard: the chosen value is compared with the instrument table for a standard contract (XAU $10 per
  0.10 pip, forex majors ~$10/pip/lot). More than ×3 off either way → table value used, WARNING logged, ops alert
  `pip_value_anomaly` (high, dedupe per account+symbol), `pip_value_source` (`broker_spec` | `account_type_table` |
  `table_override`) + `pip_value_anomaly` stamped on the signal, the decision-ledger snapshot and the decision
  context stage `sizing`. Paper accounts keep their microcent convention (guard targets live broker specs).
* (c) `risk.raw_lot_anomaly()`: raw lot > max_lot × 10 BEFORE any trim → pulse "Sizing anomaly: raw lot … —
  trade rejected", signal `sizing_rejected`, counter `sizing_anomaly_block`. No clamp-down.
* (d) `tests/unit/test_v1_60_54_pip_value_sanity.py` (11): STARTRADER / OnEquity / VTMarkets / RoboForex gold
  specs, the 16 Oct incident ($0.01 → $10, 25 lots → 0.25), cent spec override, EURUSD spec + mislabelled
  account, within-×3 kept, paper exemption, raw-lot hard reject placement.
* Constants: `PIP_VALUE_SANITY_RATIO = 3.0`, `RAW_LOT_ANOMALY_MULT = 10.0` (risk.py). No new env vars.

## #43 — VaR deleverage (replaces #37) + close-command watchdog + breakeven_trigger_r (HIGH)
**(a) VaR inputs audited.** VaR notional never used pip_usd (so #42 did not inflate it); the real gaps were:
* `portfolio.sectors.sector_for("GOLD#")` → "other" (cap 100 %!) — now normalised with `base_symbol()` → commodity;
  `_sector_exposure` uses `instruments.notional_usd` (GOLD# / XAUUSD+ / forex) instead of `lot × price`.
* `open_risk_usd` falls back to `original_stop_loss` when the live stop is not mirrored yet; one stop-less position
  no longer unbounds the whole book (`_mixed_bounded_loss_usd`: stop distance for stopped rows, parametric share
  for the rest).
* `var_cap_exempt_reason`: a book whose stop-bounded loss (× gap 1.5) is inside the VaR cap is never deleveraged,
  in addition to the v1.60.52 single-position / breakeven exemptions. Tests: 0.05-lot XAUUSD with a $5 or $25
  stop on $10k (and on $1k) never breaches; VaR identical for XAUUSD vs GOLD# / microcent label.
**(b) Deleverage closes are now acknowledged.** `execute_deleveraging_actions` issues the close as a fenced
`FULL_CLOSE` modification (`intent_id`, `emergency=True` — the path scalp/revive already use and the EA acks),
instead of the bare trades-block `close_requested` flag that produced "requested → superseded" five times with no
terminal ack. The FULL_CLOSE ack handler records `terminal_ack` on the `close_commands` row
(`close_commands.record_terminal_ack`); a terminal failure marks the row `failed` and releases the close.
New `close_watchdog.sweep` (bot tick): requested command with no terminal ack after `CLOSE_UNACKED_ALERT_SEC`
(default 60) → ops alert `close_command_unacked` (high, once per command) + pulse.
**(c) A pending/failed close no longer freezes management.** After `CLOSE_REQUEST_TTL_SEC` (default 300) without
broker confirmation the request EXPIRES (`close_commands.expire_close`): `close_requested=False`, the FULL_CLOSE
modification is cleared, ledger row `expired`, trade `close_request_expired{key,at,reason}`, ops alert
`close_command_expired`, pulse "close request expired — management resumed". A terminal-failure ack expires it
immediately. The position stays open and visible; nothing is retried blindly.
**(#8 widened) `breakeven_trigger_r` is a real knob again** (BotConfig, default **1.0 × R**, 0 = TP1-only,
removed from DEAD_CONFIG_FIELDS): `trade_manager._breakeven_trigger_r` moves the stop to entry at
+N × sl_pips for every NON-scalp engine (breakout signals carry TP1 = TP, so the TP1 break-even never fired —
600007662145 reached +2.45R and was scratched). Never loosens, honours be_clamped, never re-arms; scalp engines keep
their fixed +0.5R (+costs) rule. Partial/trailing fields stay removed (v1.60.48). Bot Config UI: "Break-even
trigger (non-scalp engines)". Presets' existing `breakeven_trigger_r` values (0.4–1.0) are now honoured.
**Tests** `tests/unit/test_v1_60_54_var_deleverage_breakeven.py` (10), `tests/integration/test_v1_60_54_close_watchdog.py`
(5, real Mongo, fake-EA acks: fenced FULL_CLOSE issued · silent EA → alert at 61 s, expiry at 301 s, slot free ·
terminal failure releases immediately · success ack recorded, no false alert).
**New env (all optional):** `CLOSE_UNACKED_ALERT_SEC=60`, `CLOSE_REQUEST_TTL_SEC=300`. **New config:** `breakeven_trigger_r=1.0`.
