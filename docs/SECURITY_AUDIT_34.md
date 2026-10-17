# Security Audit #34 — v1.60.43 diff + regression sweep

**Verdict: PASS — no P0–P2.** Scope: the new admin EA-fleet panel/endpoint, EA-authenticated candle/ack writes,
authority alerting, the shared economic-calendar cache, worker lease telemetry, release-truth file reads, the new
deploy scripts (`pull_with_retry`, `gate_python`, `restore_release_truth`, `record_held_live`, `reanchor-audit-chain.sh`),
the internal drill probe target, the CI dependency publisher, the fleet UI; plus a sweep of auth, `/api/ops` gating,
bridge-token auth, CORS and body limits.

## Verified controls
- `GET /api/ops/ea-fleet` — `_ops_actor` (METRICS_TOKEN or admin **and** enrolled 2FA); account ids/labels/servers/hashes
  reach that actor only; per-account `ea_feed_events` scan only for candidate-version terminals.
- Bridge ack `ea_cmd_stats` `$inc` targets the token-authenticated account; trade ownership checked; error strings bounded.
- Shared calendar cache is written only by the worker from the external feed; UI renders through React escaping.
- Loop/swallow-counter keys derive from code (function/component names), not request input; writes bounded by loop count.
- `deployment_state.json` is read from the fixed read-only state mount; scalar fields only.
- `record_held_live` uses a quoted heredoc with values via `os.environ` (no shell interpolation); wrapper passes `"$@"` as argv;
  both operator-run only. `internal_api_base` is environment-derived (no user-driven SSRF). Publisher never prints the token.
- CORS: credentialed requests only with the explicit production allowlist; `RequestBodyLimit` caps all bodies.

## Hardening (applied)
- **P3** `pending_modification.last_error` now truncated to 200 chars like the other error fields (`bridge_routes.py`).
- **P3** M1 feed telemetry coalesced into **one hourly bucket per account** (`ea_feed_events` upsert with `$inc payloads/bars`)
  instead of one document per payload; the panel sums `payloads`. 48 h TTL unchanged.
- **P3 (noted)** the ea-fleet panel exposes broker server/label/hash to the admin/ops scope — keep it admin-gated.

## Limits
Static review + git history; JWT/step-up/OAuth internals and legacy routes not exhaustively re-traced this round
(audits 13–33 open P0–P2: none).
