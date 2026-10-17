# EA v1.66 — candidate release runbook + install guide (#81, v1.60.61)

**Scope (frozen):** server-requested candle history backfill. The poll-trades response may carry
`"candles_request":{"symbol":"XAUUSD","timeframe":"H4","bars":300}`; the EA answers with `CopyRates(sym, PERIOD_H4, 1, n)`
(completed bars only) posted to the existing `/api/bridge/candles` endpoint with `timeframe: "H4"`.
**Not in scope:** order send, close, partial close, intent/seq fencing, NL close fence, journal, heartbeat — byte-identical
to 1.64/1.65 (`backend/tests/unit/test_ea_1_65_candidate.py` pins their hashes; still green on 1.66).

Why: gold_trend_pulse needs 200 completed H4 bars for its EMA50 daily bias; 1.65 streams M15 + M1 only, so the server
could only resample ≈ 50 H4 bars and the bias stayed "NONE — H4 warm-up 50/200" (no GTP decision was ever recorded).

## Compatibility

| | server ≥ v1.60.61 | older server |
|---|---|---|
| **EA 1.65** | never receives `candles_request` (capability `h4_history_v1` absent) → unchanged; GTP bias stays NONE until the terminal is updated | unchanged |
| **EA 1.66** | answers `candles_request` (once at the first poll when the history is short, then daily); everything else as 1.65 | never asked → behaves exactly like 1.65 |

Capability flags are derived from the heartbeat `ea_version` (`backend/ea_capabilities.py`: `h4_history_v1` → 1.66).
1.64 and 1.65 stay fully LIVE-capable (`command_fencing_v1`, `nl_close_fence_v1`).

**Bandwidth:** one history answer ≈ 300 bars ≈ 22 KB, at most once per 60 s locally (server re-asks a dropped answer after
10 min, then daily). Server validates (OHLC geometry, broker-day-grid alignment, 150-day window), merges by bar time, keeps
800, stamps `backfill_at` on `intraday_candles {XAUUSD, H4}`.

## Release path (signed EX5 → candidate → canary → soak → promote)

1. **Compile + sign** — the `ea-release` workflow runs on the merge to `main` (MQ5 path trigger) or Actions → `ea-release` →
   *Run workflow*. **No human capture:** `docs/RELEASE_HASHES.json#ea`, `release/ea_release.json` and the EX5 are written
   only by the bot (protected-paths, v1.60.47). On the PR branch the MQ5 version is simply ahead of the recorded 1.65:
   `check_release_hash_drift.py` prints *pending re-release* (WARN) and `verify_ea_release.py --check` fails closed on the
   version mismatch (expected until the bot commit — `test_3_gate_runs_in_an_empty_shell_environment` asserts exactly that).
   On merge the bot's `verify_ea_release.py --sign` **adopts the forward bump**: it captures the new MQ5 hash, records
   1.66 and carries the signed 1.65 as `previous` (a changed MQ5 under the *same* version is still refused). It commits
   `release/ea_release.json`, `docs/RELEASE_HASHES.json`, the EX5 back to `main`. **Tag v1.60.61 only after that bot
   commit** — the server fails closed (`EA_RELEASE_HASH_UNPINNED` for LIVE-attested accounts) while the shipped MQ5 version
   has no signed record. CI gates `scripts/check_ea_structure.py` (version 1.66, balanced braces, frontend constants in
   sync) and `scripts/check_ea_fleet_compat.py` (the record must still accept the 1.65 EX5 the fleet runs) must be green.
2. **Candidate channel** — the new EX5 lands in `candidate`; `stable` keeps **1.65** for instant rollback.
3. **Canary** — RoboForex Demo first (Admin → EA Release Channel → canary agents). Install 1.66 (guide below). Soak **24 h**:
   heartbeats show `1.66`, Experts tab logs `STOIC history backfill: sent 300 H4 bars for XAUUSD…` within one poll of the
   first heartbeat, Bot Health → Risk State shows **GTP: H4 bars ≥ 200/200 (ea_h4+m15)** and the GTP state's bias leaves
   NONE at the next 15-min refresh; 0 deploy failures; no change in order/close behaviour.
4. **Other terminals** — one at a time after the canary passes (check the heartbeat version after each).
5. **Promote** — only after the soak with 0 failures (Admin → *Promote candidate*, step-up re-auth, audited). The previous
   stable (1.65) remains one click away.

## Install guide — replacing the EA in MetaTrader 5 (per terminal)

1. Download the signed `EmergentTradingBridge.ex5` (Dashboard → Accounts → *EA download*, or `GET /api/setup/ea-download`).
   Check its sha256 against `release/ea_release.json → ex5_sha256`.
2. MT5: **File → Open Data Folder → MQL5 → Experts**. Rename the existing `EmergentTradingBridge.ex5` to
   `EmergentTradingBridge-1.65.ex5` (local rollback), copy the new file in.
3. **Navigator** → right-click *Expert Advisors → Refresh*.
4. Chart → *Expert List* → remove the old instance, drag the refreshed `EmergentTradingBridge` onto the same chart. Inputs
   unchanged (no new inputs in 1.66; `ServerUrl` / `BridgeToken` / `InstallationId` auto-load from `MQL5\Files\STOIC-*.txt`).
   Tick **Allow Algo Trading**; toolbar **AutoTrading** green.
5. **Verify:** Accounts row shows **EA 1.66** within ~60 s; for a gold-streaming terminal the Experts tab logs the history
   backfill line on the next poll and Bot Health shows `GTP: H4 bars n/200` climbing to ≥ 200.
6. **Rollback (local):** remove the EA, rename `EmergentTradingBridge-1.65.ex5` back, refresh, re-attach.
   **Rollback (fleet):** Admin → EA Release Channel → *Rollback to previous stable*.

## Server-side verification checklist (candidate soak)

- `POST /api/bridge/poll-trades` for a 1.66 account that streams XAUUSD M15 returns `candles_request` once (then
  `accounts.h4_history_request.dispatched_at` is set and the key is absent for 10 min); a 1.65 account never gets the key
  (`test_pending_request_only_for_capable_gold_streaming_terminals_and_marks_dispatch`).
- `intraday_candles {symbol: XAUUSD, timeframe: H4}` exists with ≥ 250 bars, `backfill_at` set, `t_basis: utc`.
- `gtp_state.h4_bars ≥ 200`, `h4_source == "ea_h4+m15"`, bias reasons no longer start with "H4 warm-up".
- Bot Health → Risk State → "GTP: H4 bars" is **pass**.
