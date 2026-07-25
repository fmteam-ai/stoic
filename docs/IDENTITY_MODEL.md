# STOIC Identity Model — Presentation vs. Authority

**Rule (adopted codebase-wide, iter-124):**

> **User-entered values are presentation-only. Broker-reported values are
> authoritative.**

## The two identities

### 1. User-defined display values — PRESENTATION ONLY
Examples: account `label` ("My FTMO Account", "IC Markets Gold"), nicknames,
descriptions.

Allowed uses: UI display, filtering, search, dashboards, human-readable
report/notification text.

**Never** use them as a system identifier: users can rename them, two can be
identical, uniqueness is not guaranteed, and they prove nothing about which
MT5 account is actually connected.

### 2. Broker-verified identity — AUTHORITATIVE
The combination STOIC trusts, reported by the MT5 terminal after login:

| Field | Example | Source |
|---|---|---|
| `account_number` / `broker_account_id_reported` | `12345678` | MT5 login (EA heartbeat) |
| `broker_server` (`accounts.server`) | `ICMarketsSC-Live27` | MT5 terminal |
| `terminal_build` | `4910` | EA heartbeat (v1.55+) |
| `installation_id` | `inst_8fd31…` | Pairing claim |

Required uses: **pairing, execution authorization, heartbeat validation,
affiliate attribution where trading identity matters, reconciliation,
auditing, risk management.**

## Where this is enforced

- **EA pairing** (`vps_agent.claim_pairing_code`): binds
  `installation_id → terminal → host`; `permitted_account` returns the
  broker account number, never the label.
- **Execution authority** (`execution_leases`): lease =
  `installation_id + broker_server + account_number`. Another VPS can never
  claim authority because a label matches.
- **Heartbeats** (`vps_agent.verify_heartbeat_identity`,
  `bridge_routes.heartbeat`): authoritative only when installation is
  recognized → bound to the account → broker server matches → login matches
  → installation holds the execution lease. Otherwise balance/equity are
  nulled and `ea_identity.authoritative=false`.
- **Trade records** (`execution.broker_identity_snapshot`): every trade
  stamps `broker_identity {account_number, broker_server, broker,
  installation_id}` at open — reconciliation and audits survive renames and
  account-doc deletion. Crypto trades stamp `exchange_id` +
  `api_key_fingerprint`.
- **Promotion evidence** (`operational_modes.promotion_gate`): broker
  certification entries carry `account_id + account_number + broker_server`;
  the label rides along as `account_label` for display only.
- **Account dedupe** (`account_routes` create): cross-user collision guard
  keys on `broker + account_number` (live heartbeat), never on label.
- **Affiliate attribution**: commissions key on
  `affiliate_id → user_id → session_id (subscription payment)`. If payouts
  ever depend on funded/active accounts or trading volume, attribution MUST
  key on the trade `broker_identity` snapshot — never labels.

## Rules for new code

1. Query/match/dedupe accounts by `_id`, `account_number + server`, or
   `installation_id` — never by `label`.
2. Any record used for money, authority, or audit must stamp the verified
   identity at write time (immutable snapshot, not a lookup).
3. `label` may appear in user-facing strings; when a message drives an
   operational decision (promotion blockers, alerts feeding automation),
   include the broker account number.
4. Broker-REPORTED values win over user-TYPED expectations: mismatches are
   surfaced (never silently adopted), and unverified reports are treated as
   non-authoritative.
