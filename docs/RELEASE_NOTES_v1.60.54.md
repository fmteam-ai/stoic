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
