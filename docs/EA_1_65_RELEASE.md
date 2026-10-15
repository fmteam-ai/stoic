# EA v1.65 — candidate release runbook + install guide

**Scope (frozen):** (a) `MODIFY_SL` may carry `new_tp` so the broker TP follows a fill re-base; the ack reports
`confirmed_position_tp`. (b) M1 bar stream for the scalp (tick-stream) symbol, batched once per minute.
**Not in scope:** order send, close, partial close, intent/seq fencing, NL close fence, journal — byte-identical to 1.64
(`backend/tests/unit/test_ea_1_65_candidate.py` pins their hashes).

## Compatibility

| | server ≥ v1.60.40 | older server |
|---|---|---|
| **EA 1.64** | `new_tp` is never sent (capability `modify_tp_v1` absent) → plain MODIFY_SL, TP untouched | unchanged |
| **EA 1.65** | gets `new_tp` when a fill re-base needs it; streams M1 | `new_tp` absent → TP passed through (1.64 path); M1 posts go to the existing `/api/bridge/candles` and are stored like any timeframe |

Capability flags are derived from the heartbeat `ea_version` (`backend/ea_capabilities.py`: `modify_tp_v1`,
`m1_stream_v1` → 1.65). 1.64 stays fully LIVE-capable (`command_fencing_v1`, `nl_close_fence_v1`).

**Bandwidth / heartbeat:** the M1 batch runs on the slow lane *after* `SendHeartbeat()` + `PollPendingTrades()`,
at most once per `M1StreamSeconds` (60 s), `M1StreamBars` (30, clamped 5–120) bars ≈ 2.5 KB per minute per terminal.
Server dedupes by bar time, drops the forming bar, keeps 26 h (1600 bars), records `gap_bars`.

## Release path (signed EX5 → candidate → canary → soak → promote)

1. **Compile + sign** — GitHub → Actions → `ea-release` → *Run workflow* (branch `main`). The sanctioned MetaEditor
   compile produces `backend/static/EmergentTradingBridge.ex5`, records `release/ea_release.json` (mq5/ex5 sha256,
   compile log, source commit) and signs it with the external signer. CI gate `scripts/check_ea_structure.py` must be
   green (it is: version 1.65, balanced braces, frontend constants in sync).
2. **Candidate channel** — the new EX5 lands in the `candidate` channel; `stable` keeps **1.64** for instant rollback
   (Admin → Bot Health → EA Release Channel → *Rollback to previous stable*, or `POST /api/ops/releases/rollback`).
3. **Canary** — `POST /api/ops/releases/canary` with the **RoboForex Demo** agent id only (Admin → EA Release Channel →
   canary agents). Install 1.65 on that terminal (guide below). Soak **24 h**: 0 deploy failures, heartbeats show
   `1.65`, `/api/ops/news-sources`/Bot Health unaffected, scalp `candle_feed_health` shows M1 payloads with
   `forming_dropped ≥ 0` and no `last_error`, and a fill re-base produces a MODIFY_SL ack with `confirmed_position_tp`
   equal to the trade's `take_profit`.
4. **Other three terminals** — after the 24 h canary passes, install on the remaining terminals (one at a time, check
   the heartbeat version after each).
5. **Promote** — only after the soak with 0 failures: `release_channels.promotion_status()` all green (soak window
   elapsed, 0 corroborated deploy failures, signed EX5 record present). Admin → *Promote candidate* (step-up re-auth, audited) or the
   auto-promotion in the reconciliation worker. The previous stable (1.64) remains one click away.

## v1.60.41 — recovery from the 14 Oct `previous: null` record (operator steps, on `main`)

What happened: the merge set `docs/RELEASE_HASHES.json#ea` to an unsigned 1.65 capture, which discarded the signed
1.64 record; `ea-release` (41893f48) therefore signed 1.65 with `previous: null`; the new server rejected the stable
1.64 EX5 (`b05ed821…`) on all 4 terminals, voided their DEMO attestations and set every account `CLOSE_ONLY`.
The deploy was rolled back. Guards now in place: `capture_release_hashes.py` never discards a signed `ea` entry
(carried as `previous`); `verify_ea_release.py` takes `previous` from `release/ea_release.json` / `#ea` / `#ea.previous`
and **fails** on a version change without a signed prior record (`--no-previous-reason` is the only, recorded,
bypass); `scripts/check_ea_fleet_compat.py` blocks the tag (`release.yml` tag-only job, `ci.yml`, `ea-release`) and the
host update (`deploy/update.sh`, against the PREV checkout's record) when the stable EX5 is not accepted.

1. Restore the signed 1.64 record from the last good ea-release commit — never hand-edit the `ea` entry:
   `git checkout 3344609d -- release/ea_release.json docs/RELEASE_HASHES.json && git commit -m "restore signed EA 1.64 record"`
   (then `python scripts/capture_release_hashes.py` on the same commit: it keeps the signed 1.64 as `#ea.previous`
   and records the 1.65 MQ5 as the pending capture; commit `docs/RELEASE_HASHES.json`).
2. Actions → `ea-release` → Run workflow (`main`). The record step must print `OK: EX5 … · signed` and the fleet gate
   `OK: release EA 1.65 accepts the fleet stable EX5 b05ed821… (signed previous)`; the bot commit's
   `release/ea_release.json` must show `previous.version == "1.64"` and `previous.ex5_sha256 == b05ed821…`.
   The fleet gate derives "stable" from the newest **shipped** tag: `v1.60.40` is listed in
   `release/rolled_back_tags.json` (never live) and is skipped, so the gate compares against `v1.60.39` (1.64,
   `b05ed821…`). If a recovery ever needs it, the repository variable `EA_FLEET_STABLE_SHA` (Settings → Variables)
   overrides the derivation with the EX5 the terminals actually run; on the host, `deploy/releases.log` rollback
   entries are honoured automatically.
   v1.60.42: on the VPS the gate is run by `deploy/lib.sh gate_python` (the first host interpreter ≥ 3.9 with
   `cryptography`); its imports are 3.9-safe and a verification that cannot run is reported as `cannot verify: …`
   (a gate problem) — never as an invalid record. A refused gate now re-publishes the running release's
   attestation (`restore_release_truth`), so `/api/health` keeps reporting the version that actually runs.
3. Tag `v1.60.42`. The `tag-only` job runs the fleet gate against the previous shipped tag; `deploy/update.sh` runs it again on
   the host before building. Expected after the deploy: all 4 terminals keep `attested DEMO` / `FULL` while still on 1.64.
4. Re-attest: the four accounts whose automatic DEMO attestations were voided re-attest automatically on the next
   heartbeat once the server accepts `b05ed821…` again (`auto_attest`: broker demo + signed EX5 + identity chain).


## Install guide — replacing the EA in MetaTrader 5 (per terminal)

1. Download the signed `EmergentTradingBridge.ex5` for the candidate (Dashboard → Accounts → *EA download*, or
   `GET /api/setup/ea-download`). Check its sha256 against `release/ea_release.json → ex5_sha256`.
2. In MT5: **File → Open Data Folder → MQL5 → Experts**. Rename the existing `EmergentTradingBridge.ex5` to
   `EmergentTradingBridge-1.64.ex5` (keep it — this is your local rollback), then copy the new file in.
3. In the **Navigator** panel right-click *Expert Advisors → Refresh*.
4. On the chart that runs the EA: right-click → *Expert List* → remove the old instance, then drag the refreshed
   `EmergentTradingBridge` onto the same chart. Inputs: leave `ServerUrl`, `BridgeToken`, `InstallationId` as before
   (they auto-load from `MQL5\Files\STOIC-*.txt`); the new inputs `M1StreamEnabled = true`, `M1StreamSeconds = 60`,
   `M1StreamBars = 30` can stay at their defaults. Tick **Allow Algo Trading** in the *Common* tab.
5. Make sure the toolbar **AutoTrading** button is green (on). The chart's top-right shows a smiling icon / "STOIC".
6. **Verify:** within ~60 s the Dashboard → Accounts row shows **EA 1.65** (heartbeat `ea_version`), the Experts tab
   logs `STOIC v1.65 …`, and Admin → Bot Health → News Sources / Safety Loops stay green. Scalp accounts: Diagnostics →
   candle feed shows `timeframe M1` payloads arriving once a minute.
7. **Rollback (local):** remove the EA from the chart, rename `EmergentTradingBridge-1.64.ex5` back, refresh, re-attach.
   **Rollback (fleet):** Admin → EA Release Channel → *Rollback to previous stable*.

## Server-side verification checklist (candidate soak)

- `GET /api/bridge/poll-trades` for a 1.65 account includes `new_tp` only when a fill re-base queued one; for a 1.64
  account `new_tp` is always `null` (`test_capability_flags_let_1_64_and_1_65_coexist`).
- Modification ack with `confirmed_position_tp` → trade `take_profit` / `confirmed_take_profit` / `tp_confirmed_at`.
- `intraday_candles {timeframe: "M1"}` grows to ≤ 1600 bars, `gap_bars` small during market hours.
- Deferred-shadow replay reports `shadow.timeframe == "M1"` for new rows (`m1_bars_after` prefers the EA feed).
