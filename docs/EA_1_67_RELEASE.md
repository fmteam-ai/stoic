# EA v1.67 — install guide (#86, v1.60.64)

**Scope (frozen):** heartbeat spreads for broker-renamed symbols. `BuildSpreadsJson` resolves each `TrackedSymbols` entry
to the broker's own name (`XAUUSD+`, `XAUUSD.crp`, `XAUUSDm`, `GOLD#`) and always includes the chart symbol; `SymbolPipSize`
matches by prefix like `pip_utils.base_symbol`. **Not in scope:** order send, close, partial close, intent/seq fencing,
NL close fence, journal, heartbeat payload shape — the 1.64 frozen function hashes still pass.

| | server ≥ v1.60.64 | older server |
|---|---|---|
| **EA 1.66** | works; renamed gold symbols still report no spread (Bot Health warns "Update the EA to 1.67") | unchanged |
| **EA 1.67** | gold spread reported under the broker name; server adds the `XAUUSD` key | the raw broker key is stored; base readers fall back to `spread_at_entry`'s base matching |

## Release path
1. Merge to `main` → the `ea-release` workflow compiles and signs 1.67 (bot commit records 1.67, previous 1.66).
2. Tag `v1.60.64` only after that bot commit and a green `verify_ea_release.py --check` on main.
3. Deploy, then update each terminal (one at a time): dashboard **Accounts → Repair** (or manual copy of the signed EX5 into
   `MQL5\Experts`, Navigator → Refresh, re-attach on the gold chart, inputs unchanged).
   **Check there is only ONE EA instance per terminal** (a second chart with the EA causes attestation flapping).
4. Verify per terminal: Accounts row shows **EA 1.67**; Bot Health → Connectivity → "Spread reported for traded symbols"
   passes; `current_spreads` contains the broker gold key **and** `XAUUSD`.

## Rollback
Local: re-attach the previous EX5. Fleet: the signed record keeps 1.66 as `previous`, so 1.66 terminals stay accepted.
