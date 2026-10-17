# v1.60.64 — #86 Gold spread on broker-renamed symbols (EA 1.67) + spread-history sampling fix

**No change to order sending, exits, fencing or any config/flag. `spread_gate_mode` stays shadow.** `requirements.txt`
untouched. EA 1.67 must be compiled + signed by the `ea-release` workflow on merge (tag only after the bot commit records
1.67 and `verify_ea_release.py --check` passes on main).

## Why
Live check 17 Oct 17:32 UTC: `accounts.current_spreads` had **no gold key on 3 of 4 accounts** — OnEquity (`GOLD#`),
STARTRADER (`XAUUSD+`), VTMarkets (`XAUUSD.crp`); only RoboForex (`XAUUSD`) reported 0.7 pips. EA ≤ 1.66 `BuildSpreadsJson`
called `SymbolInfoTick` on the raw `TrackedSymbols` names (`"XAUUSD,BTCUSD"`) and silently skipped names the broker does
not have; `SymbolPipSize` was keyed on exact names. Every `current_spreads` reader — `entry_costs.spread_at_entry`,
`adaptive_sizing`, the `bot_runner` spread filter and EV cost, and the #85 cost ledger / spread gate — had no gold spread
on those accounts. Separately, #85's `record_spread_samples` pushed every heartbeat (≈ 5 s) with `$slice -60`, keeping only
the **last ~5 minutes** of each UTC hour.

## A) EA 1.67 (`backend/static/EmergentTradingBridge.mq5`)
- `BuildSpreadsJson`: each `TrackedSymbols` entry is resolved to the broker name with the new `ResolveBrokerSymbol`
  (exact → suffixed variant, strict then relaxed boundary → alias `GOLD` / `SILVER`; `""` when the broker has none, never a
  wrong symbol); the chart symbol `_Symbol` is always added; duplicates skipped; resolution cached for 5 minutes.
- `SymbolPipSize` matches the upper-cased broker name by **prefix**, exactly like `pip_utils.base_symbol`
  (`XAUUSD+`, `XAUUSD.crp`, `XAUUSDm`, `GOLD#` → 0.10). A test mirrors the EA table against `pip_utils.pip_size`.
- Unchanged: the frozen 1.64 functions incl. `SendHeartbeat` (still `"spreads": BuildSpreadsJson()`), order send / close /
  fencing / journal. Capability `spreads_resolved_v1` = 1.67. 1.66 terminals keep working (gold spread still missing there).

## B) Backend
- Heartbeat (`bridge_routes.heartbeat`): `entry_costs.normalize_spread_keys` keeps the broker keys and adds the base key
  (`GOLD#` → also `XAUUSD`; an existing base key is never overwritten), so every `current_spreads[<base>]` reader finds it.
  `execution_timing.record_spread` and `trade_costs.record_spread_samples` get one value per base (`spreads_by_base`).
- `trade_costs.record_spread_samples`: one sample per account+symbol per **UTC minute** under `minutes.<hour>.<mm>`
  (`$set`, multi-worker safe, natural 60/hour cap) — the 60 samples now cover the whole hour. Readers merge the new map with
  the legacy `hours.<h>` lists written by v1.60.63.
- Bot Health → Connectivity → **"Spread reported for traded symbols"**: warns per live account when a traded symbol's market
  is open and its spread is missing or older than 10 minutes; on EA < 1.67 the fix is "Update the EA to 1.67".

## Tests
`tests/unit/test_v1_60_64_review_86_spreads.py` (8): EA resolver/chart-symbol/no-duplicates/frozen heartbeat; EA pip table
mirrors `pip_utils.pip_size` for 13 broker names; key normalisation; heartbeat wiring + capability; 720 heartbeats → 60
per-minute samples covering minute 00…59; legacy + mixed docs read; Bot Health coverage (missing on 1.66 GOLD#, stale,
market closed). Version asserts moved 1.66 → 1.67 in the existing EA/version tests; #85 history test reads `minutes`.

## Rollout
1. Merge → `ea-release` compiles/signs 1.67 (bot commit). 2. Tag v1.60.64 → deploy. 3. Install EA 1.67 on each terminal
(dashboard Repair or manual) — check Experts tab and that Bot Health shows the gold spread for all 4 accounts.
4. After ~2 weeks of complete spread data, review the #85 shadow scoreboard before any `enforce`.
