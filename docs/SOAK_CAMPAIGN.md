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
- checkpoint coverage **100 %** — one checkpoint per campaign day, no gaps
- **zero critical incidents** (any critical ⇒ immediate FAIL)
- **≤ 2 major incidents**
- no RED checkpoints — a day is GREEN only when ALL invariants hold:
  campaign account connected, **no duplicate executions** (signal/ticket),
  **no unconfirmed ghost trades** (reconciliation), latency **UNKNOWN
  rate ≤ 20 %**, **no version drift** vs the versions frozen at start,
  and no platform-critical subsystem failing (global health is reported
  separately and only critical failures gate)

## Incident severity taxonomy (formal)
| Severity | Definition | Effect |
|----------|------------|--------|
| critical | Money-impacting/trust-destroying: wrong or duplicate execution, unreconciled position, data loss, security breach | ONE fails the campaign |
| major | Capability degraded: missed trading window, subsystem failing > 1h, repeated rejects | > 2 fail the campaign |
| minor | Transient/cosmetic with automatic recovery | recorded, never gating |

## Immutable Production Evidence
Every checkpoint appends a hash-chained record (campaign, day, full
checkpoint, release fingerprint) to `production_evidence`.
`GET /api/ops/soak/evidence` returns the chain + `chain_valid` —
any tampering breaks verification. Material versions (EA + backend
release hash) are frozen at `soak/start`; drift marks the day RED.

The status endpoint transitions the campaign to PASS/FAIL automatically
and the whole evidence trail (checkpoints + incidents) stays queryable.

## Test suite hooks
- `pytest -m chaos` — fault-injection drill battery (never ordinary CI)
- `pytest -m soak` — live-environment soak checks (never ordinary CI)
