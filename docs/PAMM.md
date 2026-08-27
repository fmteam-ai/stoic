# PAMM — Managed Money (Percent Allocation Management Module)

> Full technical reference for STOIC's PAMM subsystem.
> Audience: developers, operators, and admins.
> Source of truth: `/app/backend/modules/pamm/` + `/app/backend/services/broker_gateway/`.

---

## 1. Design philosophy

**The broker owns the money. STOIC owns strategy, risk, and reporting.**

STOIC never computes investor balances, never holds funds and never performs
allocation math. All money movement (investor onboarding, allocation,
NAV) is executed by the **broker's PAMM engine** through a partner adapter.
STOIC mirrors the broker's authoritative state, gates whether trading is
*allowed*, and provides the risk/safety/audit layer above it.

```
Investor ──► Broker PAMM engine (money, allocations, NAV)
                    ▲
                    │ adapter (REST / MT5 Manager / sandbox)
                    ▼
STOIC PAMM module ── risk gates · op-states · reconciliation ·
                     position truth · dual auth · marketplace ·
                     events · reports · incidents
```

---

## 2. Roles & permissions

| Role | How granted | Can do |
|---|---|---|
| **Investor** (any authenticated user) | default | Browse marketplace, submit join requests, view own requests |
| **Manager** | Admin grants `pamm_manager: true` via `POST /api/pamm/managers` (step-up MFA) | Everything on programs they manage: detail, pause, investors, NAV, risk limits (tighten only), publish, join-request decisions, reconcile |
| **Admin** | `role: admin` on the user document | Everything, on all programs, plus: create programs, clear e-stop/risk-breach, partners, certification, dual-auth decisions, drift tolerance |

Enforcement helpers (`modules/pamm/permissions`):
- `require_manager(db, user)` — manager or admin.
- `require_program_access(db, user, program)` — admin, or the program's `manager_id`.
- `require_admin(user)` — admin only.

**Step-up MFA** (`_step_up`): every *risk-increasing* mutation demands fresh
TOTP verification and writes an audit event: resume, clear e-stop, clear
risk-breach, manager grant, risk-limit edits, op-state de-escalation,
change-request create/decide, partner registration.

**Rate limits**: all mutations pass `pamm_mutate` (30/min/user); health checks
`pamm_health` (10/min); joins `pamm_join` (5/min); webhooks 600/min/partner.

---

## 3. Data model (MongoDB collections)

| Collection | Contents |
|---|---|
| `broker_partners` | Registered broker adapters (`prt_…`), vault-encrypted credentials |
| `broker_servers` | Broker server metadata |
| `pamm_programs` | Program master docs: `program_id (pgm_…)`, `broker_program_id`, `manager_id`, `partner_id`, `status`, `trading`, `op_state`, `emergency_stop`, `risk_limits`, `risk_breach`, `published`, `aum`, `last_nav`, `drift_tolerance`, `position_truth` |
| `pamm_master_accounts` | Program → broker master login mapping |
| `pamm_allocations` | Mirrored allocations (`investor_id`, `broker_allocation_id`, amount) |
| `pamm_nav_snapshots` | NAV time series (broker-authoritative) — feeds all risk math |
| `pamm_trade_history` | Mirrored master-account trades |
| `pamm_fee_periods` | Fee accrual periods |
| `pamm_reconciliation` | Reconciliation run results |
| `pamm_reports` / `pamm_audit` / `pamm_events` / `pamm_notifications` | Reporting, audit trail, event stream (idempotent via unique `event_key`), operator alerts |
| `pamm_health` | Partner heartbeat history (7-day retention) |
| `pamm_join_requests` | Marketplace funnel (`jrq_…`, status pending/processing/approved/rejected) |
| `pamm_change_requests` | Dual-authorization requests (`chg_…`, 48h expiry) |
| `pamm_incidents` | First-class incidents (`flatten_failed`, `position_drift`) |
| `pamm_expected_positions` | STOIC's expected book per program (unique per program) |
| `pamm_position_truth` | Position-truth check history |

Indexes are created idempotently at startup by
`modules/pamm/models/ensure_pamm_setup(db)` (also seeds the sandbox partner;
a mock REST demo partner is seeded only when `APP_ENV != production`).

---

## 4. Program lifecycle

1. **Create** — admin: `POST /api/pamm/programs` `{name, manager_id?, partner_id?, currency?, manager_fee_pct?}`.
   - Sandbox partner: broker program is auto-provisioned.
   - Real partner: the program **must already exist broker-side** (matched by name) — otherwise 400.
   - Emits `ProgramCreated`; creates the master-account mapping.
2. **Operate** — manager: pause/resume, add investors, run reconciles, tune risk limits (tightening applies instantly, loosening requires dual auth).
3. **Publish** — manager: `POST /programs/{id}/publish` `{publish, pitch}` → appears in the marketplace.
4. **Grow** — investors submit join requests; manager approves → broker performs onboarding + allocation; STOIC mirrors and notifies.
5. **Protect** — the risk engine sweeps every ~60s (`sweep_once`): breach → halt or flatten, incidents escalate, operators are paged.

---

## 5. Risk engine

### 5.1 Default limits (`modules/pamm/risk/limits.py`)

| Limit | Default threshold | Action on breach | Curve |
|---|---|---|---|
| `daily_loss_pct` | 5% | halt | linear |
| `weekly_loss_pct` | 10% | halt | linear |
| `monthly_loss_pct` | 15% | halt | linear |
| `max_drawdown_pct` | 20% (from peak NAV) | **flatten** | exponential |
| `max_exposure_pct` | 200% of NAV | halt | logistic |
| `max_correlated_positions` | 3 per currency | halt | step |
| `news_filter` | high-impact, −30min/+15min blackout | blocks new trades | — |

- Loss percentages measure NAV decline from the period-start baseline
  (last snapshot before the period, else first within it).
- Broker unreachable → exposure/correlation are `null` (unknown ≠ breach),
  but `broker_uncertain` op-state logic and heartbeat incidents cover outages.
- `evaluate_program` is **pure** (no side effects). `run_risk_check`
  evaluates **and enforces**: escalates op-state (`new_trades_paused` for
  halt, `emergency_flatten` for flatten), records `risk_breach` on the
  program, emits `RiskLimitBreached`, and raises an operator notification.
- A recorded `risk_breach` latches: trading stays blocked until an admin
  clears it (`POST /clear-risk-breach`, step-up MFA).

### 5.2 Trade verdicts (`risk/verdict.py`)

`POST /programs/{id}/trade-verdict {requested_risk_pct, symbol?, side?, …}`
returns **APPROVE / REDUCE / REJECT** with a scaled `approved_risk_pct` —
never a bare boolean.

- Each enabled limit converts its utilization into a factor via its curve:
  - `linear` — full size below 50% utilization, then linear to 0 at cap
  - `exponential` — linear², increasingly conservative near the cap
  - `step` — 1.0 / 0.5 / 0.25 / 0 tiers at 50/75/90%
  - `logistic` — sigmoid centred at 75% utilization
  - `hard` — all-or-nothing at the cap
- The **minimum** factor across limits scales the request; `risk_reduced`
  op-state multiplies by 0.5; scale ≤ 0.1 → REJECT.
- REDUCE/REJECT verdicts are recorded in verdict-tracking for empirical
  verdict-vs-outcome scoring (feeds Outcome Attribution).

### 5.3 News blackout

The economic calendar (`risk/news.py`) blocks new trades within the
configured window around events at/above `min_impact`.
`GET /api/pamm/news` returns the high-impact calendar view.

---

## 6. Emergency operating states (`risk/states.py`)

```
running → risk_reduced → new_trades_paused → broker_uncertain
        → close_risk_only → emergency_flatten → locked
```

Rules:
- **Automation may only escalate** (move to a safer state).
- **De-escalation is human-only** and requires step-up MFA
  (`POST /op-state` with a lower-severity target).
- **Leaving `locked` requires dual authorization** — never a single human,
  never the AI.
- `new_trades_paused`+ blocks all new trades (`blocks_new_trades`).
- `emergency_flatten` triggers **flatten-and-verify**:
  - The close command runs through an **execution intent** (`run_once`) —
    one logical flatten executes broker-side at most once, even under
    concurrent sweeps and duplicate callbacks.
  - The book is only considered flat when the broker **confirms zero open
    positions** — the close command result is never trusted as proof.
  - Failure → `flatten_failed` incident with an escalation ladder:
    attempt 2 → critical alert · attempt 3 → formal broker incident ·
    every 5th attempt → page operator · unresolved >5min → external
    escalation. The auto-sweep retries unresolved flattens first.
- `set_op_state` is the **only** mutator of `op_state`; it syncs broker
  pause/resume and the legacy `emergency_stop` flag.

---

## 7. Position Truth (reconciliation of reality)

STOIC maintains `pamm_expected_positions` per program and compares it
against the broker book:

- `GET /programs/{id}/position-truth` — current truth status, expected
  book, 20-run history, open drift incident.
- `POST /programs/{id}/position-truth/check` — compare now; drift beyond
  `drift_tolerance` freezes trading and opens a `position_drift` incident.
- `POST /programs/{id}/position-truth/acknowledge` (admin) — adopt broker
  truth as the expected state. **Trading remains frozen** — resuming is a
  separate step-up-gated action.
- `PUT /programs/{id}/drift-tolerance` (admin) — decreases apply
  immediately; **increases route through dual authorization**.

---

## 8. Dual authorization (`dualauth.py`)

Two-person approval for anything that weakens protection. Requester and
approver **must be different admins**; requests expire after **48 hours**;
one pending request per kind per program.

Critical kinds: `risk_limits_increase`, `unlock`, `master_account_change`,
`broker_change`, `strategy_replacement`, `strategy_version_promotion`,
`leverage_cap_increase`, `max_aum_increase`, `fee_change`,
`withdrawal_rules_change`, `allocation_method_change`,
`drift_tolerance_increase`.

Loosening detection (`is_loosening`): disabling a limit, raising a
threshold, shrinking a news blackout window, or raising `min_impact`
severity all count as loosening → `PUT /risk-limits` returns
`pending_approval: true` with a `change_id` instead of applying.

Flow: `POST /programs/{id}/change-requests` → second admin
`POST /change-requests/{change_id}/approve|reject` (both step-up gated).
Approval applies the change atomically; failures revert to pending.

---

## 9. Marketplace (investor funnel)

- `GET /api/pamm/marketplace` — published + active programs with
  performance summary (any authenticated user).
- `POST /api/pamm/marketplace/{program_id}/join {amount, note?}` —
  one pending request per user per program; rate-limited 5/min.
- Manager reviews via `GET /programs/{id}/join-requests` and decides via
  `POST /join-requests/{request_id}/approve|reject`:
  - Atomic claim (`pending → processing`) makes double-clicks safe.
  - Approve → broker `create_investor` + `allocate` → mirror allocation,
    increment `investor_count`, emit `JoinApproved`, notify the requester
    in-app. Broker failure reverts the request to pending.

---

## 10. Broker partners & adapters

- `GET /api/pamm/partners` — redacted partner list (manager+).
- `POST /api/pamm/partners` (admin + step-up MFA) — register a REAL
  partner (`rest` or `mt5_manager` adapter); credentials are
  vault-encrypted at rest.
- `POST /api/pamm/partners/{id}/certify` (admin) — runs the standard
  adapter certification contract (programs, investor, allocate, positions,
  pause/resume, close-all, idempotency, error mapping). A partner must
  score 100% before production use.
- `POST /api/pamm/webhooks/{partner_id}` — broker→STOIC signed webhooks:
  HMAC signature, replay window, idempotent processing (unique
  `event_key`). Unauthenticated by design; 401 on signature failure.
- Health: `GET /health` overview + `POST /health/check` heartbeats;
  history retained 7 days.

Sandbox: `prt_sandbox` is always present for end-to-end flows without a
real broker; a mock REST demo partner exists outside production.

---

## 11. Automatic sweep

`sweep_once` (runs ~every 60s, status via `GET /api/pamm/sweep-status`):
1. Heartbeat all partners.
2. **Retry unresolved `flatten_failed` incidents first** (critical
   invariant: no silent unresolved exposure).
3. `run_risk_check` on every active program (halt/flatten + alerts).

---

## 12. Events

Emitted to `pamm_events` (idempotent) and consumed by the UI/ops:
`ProgramCreated`, `InvestorCreated`, `AllocationUpdated`, `OpStateChanged`,
`RiskLimitBreached`, `PositionsFlattened`, `FlattenFailed`,
`FlattenResolved`, `BrokerIncidentOpened`, `BrokerIncidentResolved`,
`ExternalEscalation`, `JoinRequested`, `JoinApproved`, `JoinRejected`,
`ChangeRequested`, `ChangeApproved`, `ChangeRejected`.

`GET /api/pamm/events?program_id=` (manager+) returns the recent stream.

---

## 13. API reference (all routes prefixed `/api/pamm`)

Auth legend: 🅐 admin · 🅜 manager/admin · 🅟 program access (admin or its manager) · 🅤 any authenticated user · 🔐 step-up MFA · ✌ dual auth path

### Programs
| Method & path | Auth | Notes |
|---|---|---|
| `GET /programs` | 🅜 | Admin sees all; manager sees own |
| `POST /programs` | 🅐 | Create (sandbox auto-provisions; real brokers must pre-exist) |
| `GET /programs/{id}` | 🅟 | Program + performance + trading_allowed |
| `POST /programs/{id}/pause` | 🅟 | Escalate to `new_trades_paused` |
| `POST /programs/{id}/resume` | 🅟 🔐 | 409 while emergency state engaged |
| `POST /programs/{id}/emergency-stop` | 🅟 | Escalate to emergency |
| `POST /programs/{id}/clear-emergency-stop` | 🅐 🔐 | De-escalate to paused; LOCKED → dual auth |
| `GET /programs/{id}/master` | 🅟 | Broker master account snapshot |
| `POST /programs/{id}/op-state` | 🅟 (🔐 on de-escalation) | Any of the 7 op states |

### Investors, NAV, reconciliation
| Method & path | Auth |
|---|---|
| `POST /programs/{id}/investors` `{name,email,amount}` | 🅟 |
| `GET /programs/{id}/allocations` | 🅟 |
| `GET /programs/{id}/nav` | 🅟 |
| `POST /programs/{id}/reconcile` · `GET /programs/{id}/reconciliation` | 🅟 |

### Risk
| Method & path | Auth | Notes |
|---|---|---|
| `GET /programs/{id}/risk-limits` | 🅟 | Merged limits + breach state |
| `PUT /programs/{id}/risk-limits` | 🅟 🔐 ✌ | Loosening → pending dual auth |
| `GET /programs/{id}/risk-status` | 🅟 | Pure evaluation, no side effects |
| `POST /programs/{id}/risk-check` | 🅟 | Evaluate **and enforce** |
| `POST /programs/{id}/clear-risk-breach` | 🅐 🔐 | Re-arms trading |
| `POST /programs/{id}/trade-verdict` | 🅟 | APPROVE/REDUCE/REJECT |
| `GET /news` | 🅜 | High-impact calendar |

### Position truth
| Method & path | Auth |
|---|---|
| `GET /programs/{id}/position-truth` | 🅟 |
| `POST /programs/{id}/position-truth/check` | 🅟 |
| `POST /programs/{id}/position-truth/acknowledge` | 🅐 |
| `PUT /programs/{id}/drift-tolerance` | 🅐 (increase → ✌) |

### Marketplace
| Method & path | Auth |
|---|---|
| `GET /marketplace` | 🅤 |
| `POST /marketplace/{id}/join` | 🅤 |
| `GET /marketplace/my-requests` | 🅤 |
| `POST /programs/{id}/publish` | 🅟 |
| `GET /programs/{id}/join-requests` | 🅟 |
| `POST /join-requests/{rid}/approve\|reject` | 🅟 |

### Governance & ops
| Method & path | Auth |
|---|---|
| `POST /managers` `{user_id, grant}` | 🅐 🔐 |
| `GET /programs/{id}/change-requests` | 🅟 |
| `POST /programs/{id}/change-requests` | 🅐 🔐 |
| `POST /change-requests/{cid}/approve\|reject` | 🅐 🔐 (different admin) |
| `GET /events` · `GET /incidents` · `GET /sweep-status` · `GET /health` | 🅜 |
| `POST /health/check` | 🅜 |
| `GET /partners` | 🅜 |
| `POST /partners` | 🅐 🔐 |
| `POST /partners/{pid}/certify` | 🅐 |
| `POST /webhooks/{partner_id}` | HMAC-signed (no session) |

---

## 14. Production path (certification & pilot)

1. Build/configure the real `BrokerXAdapter` (`rest` or `mt5_manager`).
2. Register the partner (admin, step-up) — credentials vault-encrypted.
3. `POST /partners/{pid}/certify` must score **100%**.
4. Broker demo environment → internal funded account → micro-PAMM with
   limited external capital → scale. (One broker at a time.)

## 15. Related documents

- `docs/ARCHITECTURE.md` — overall system architecture
- `docs/RUNBOOK.md`, `docs/INCIDENT_RESPONSE.md` — operations
- `docs/PROMOTION_CRITERIA.md` — strategy promotion gates
- In-app guide (plain-language): **/pamm-guide** (logged-in users)
