# Soak Campaign & Broker-Attached Validation Runbook

## Purpose
Prove production readiness with EVIDENCE: 14 consecutive days of
broker-attached operation, daily checkpoints, an incident log and
computed (never asserted) pass criteria.

## Prerequisites
1. A **real** broker account (not paper) attached: EA paired, identity
   verified, heartbeats flowing.
2. Validate the wiring first:
   `GET /api/ops/broker-validation?account_id=<id>` (admin) — all seven
   checks must pass (real account, verified identity, live heartbeat,
   deal history synced, ≥1 closed round-trip trade, latency traces,
   clock telemetry OK).

## Running the campaign (all admin-only)
| Step | Call |
|------|------|
| Start | `POST /api/ops/soak/start` `{"days": 14, "account_id": "...", "note": "..."}` |
| Daily checkpoint | `POST /api/ops/soak/checkpoint` (cron it daily; idempotent per day) |
| Log an incident | `POST /api/ops/soak/incident` `{"severity": "critical|major|minor", "note": "..."}` |
| Status / verdict | `GET /api/ops/soak/status` |

## Pass criteria (computed in `backend/soak_campaign.py:evaluate`)
- 14 full days elapsed since start
- checkpoint coverage ≥ 80 % of campaign days
- **zero critical incidents** (any critical ⇒ immediate FAIL)
- no RED checkpoints (degraded intelligence mode or critical incident
  on the day)

The status endpoint transitions the campaign to PASS/FAIL automatically
and the whole evidence trail (checkpoints + incidents) stays queryable.

## Test suite hooks
- `pytest -m chaos` — fault-injection drill battery (never ordinary CI)
- `pytest -m soak` — live-environment soak checks (never ordinary CI)
