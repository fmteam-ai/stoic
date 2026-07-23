# STOIC — Architecture

Event-driven quantitative trading platform: React operations console, FastAPI
core, MongoDB event/state store, MT5 Expert Advisor bridge and a CCXT crypto
bridge, with dedicated background workers behind leader leases.

## 1 · System context

```mermaid
flowchart LR
    subgraph Client
        UI[React 19 SPA<br/>operations console]
        PUB[Public share pages<br/>/p/:shareId]
    end
    subgraph Edge
        NG[nginx<br/>TLS · security headers · /api proxy]
    end
    subgraph Core
        API[FastAPI :8001<br/>REST + WebSocket /api/ws]
        W1[worker-trading]
        W2[worker-protection]
        W3[worker-reconciliation]
        W4[worker-analytics]
        W5[worker-model]
    end
    DB[(MongoDB<br/>event + state store)]
    subgraph Brokers
        EA[MT5 EA v1.53<br/>EmergentTradingBridge.mq5]
        CX[CCXT crypto bridge]
    end
    EXT[Claude · NewsAPI · Telegram<br/>Resend · Stripe · HIBP]

    UI --> NG --> API
    PUB --> NG
    API <--> DB
    W1 & W2 & W3 & W4 & W5 <--> DB
    EA -- poll /api/bridge/* --> NG
    CX --> API
    API --> EXT
```

- The **EA polls outbound** (`/api/bridge/poll-trades`, heartbeats, reports) —
  no inbound connection to the trader's machine is ever required.
- Workers coordinate through **leader leases** (`worker_leases` collection) so
  any number of replicas can run; exactly one owns each loop.
  The API refuses to double-run loops when `BACKGROUND_WORKERS_IN_PROCESS=false`.

## 2 · Order execution flow (single source of truth)

```mermaid
sequenceDiagram
    participant SIG as Signal engine / Scalp fast path
    participant GATE as Gates (permission→edge→risk→final)
    participant RES as Risk reservation
    participant OUT as Dispatch (trades doc + intent)
    participant EA as MT5 EA (v1.53)
    participant BRK as Broker
    participant REC as Reconciliation worker

    SIG->>GATE: candidate
    GATE-->>SIG: veto (recorded + shadow-labeled)
    GATE->>RES: reserve margin/risk (risk_reservations)
    RES->>OUT: dispatch with intent id + sequence fence
    EA->>OUT: poll — durable intent journal write BEFORE OrderSend
    EA->>BRK: OrderCheck preflight → OrderSend
    BRK-->>EA: deals (multi-deal fills summed by DEAL_VOLUME)
    EA->>OUT: report fills / retcodes / modifications ACK
    Note over OUT: Unresolved-Accepted state keeps the<br/>reservation until the position ticket commits
    REC->>BRK: deal replay after reconnect / restart
    REC->>OUT: broker-truth PnL, external closes, netting resolution
```

## 3 · Security layers

```mermaid
flowchart TD
    A[Request] --> B[nginx: HSTS · CSP · X-Forwarded-For = remote_addr]
    B --> C[Rate limits + lockouts<br/>rightmost-XFF keyed]
    C --> D[JWT httpOnly cookie + CSRF double-submit]
    D --> E{Live-sensitive?}
    E -- yes --> F[Step-up MFA<br/>fresh TOTP → 5-min single-use token]
    E -- no --> G[Handler]
    F --> G
    G --> H[(audit_log — append-only)]
```

Plus: TOTP 2FA + recovery codes, HIBP breached-password screening
(k-anonymity, fail-open), per-account `bridge_token` for the EA,
`X-API-Key` scoped enterprise keys, token-gated Prometheus `/api/metrics`,
production startup guard (`APP_ENV=production` forbids test bypass tokens,
requires CORS allowlist).

## 4 · Key MongoDB collections

| Collection | Role |
|---|---|
| `trades` | Order/position lifecycle; broker tickets, lifecycle_state, PnL truth flags |
| `trade_events` | Immutable event stream (OrderIntentCreated, PartialFillAdopted, …) |
| `broker_deals` | Broker-truth deal ledger (profit/commission/swap) — feeds Verified Performance |
| `trade_decisions` / `scalp_decisions` | Engine evaluations, gate verdicts, shadow outcomes |
| `risk_reservations` | Guaranteed risk/margin holds until broker commit |
| `signals` | AI signals with confidence + regime (calibration source) |
| `accounts` | Broker profiles, EA version, heartbeats, symbol specs, certification inputs |
| `bot_configs` | Per-account strategy config, trips, risk levels |
| `audit_log` | Append-only security trail (step-up, activations, panic) |
| `step_up_tokens` / `sessions` / `api_keys` | Auth artifacts (hashed) |
| `worker_leases` / `outbox` | Worker leadership + queued EA commands |
| `performance_shares` | Public verified-performance share links |

## 5 · Repository layout

```
backend/
  server.py            app wiring, middleware, WS endpoint, startup guards
  routes/              ~30 routers (auth, bot, bridge, scalp, accounts, …)
  workers/             trading / protection / reconciliation / analytics / model
  scalp/               fast-path engine, gates, order state, reservations
  static/EmergentTradingBridge.mq5   the MT5 EA (v1.53)
  tests/               2800+ tests (unit + HTTP integration)
frontend/src/
  pages/               route-level screens
  components/          panels, dialogs, shadcn/ui
  lib/api.js           axios + CSRF + silent refresh + step-up interceptor
deploy/                nginx.conf, install.sh, update.sh, backup.sh
docs/                  runbooks, DR, validation campaign, this file
.github/workflows/     ci.yml (tests, EA compile, blocking scans), release.yml (signed releases)
```

## 6 · Design invariants

1. **Broker truth wins** — deal-level resolution (`DEAL_VOLUME` sums) overrides
   any locally assumed state; estimates are flagged, never silently trusted.
2. **Every risk change is fenced** — sequence numbers fence stale EA commands;
   reservations only release on confirmed broker commit.
3. **Stopping is instant, resuming is guarded** — panic never prompts;
   releasing panic / activating live / raising risk requires step-up MFA.
4. **Append-only history** — trade_events and audit_log are never mutated.
5. **All config via environment** — no secrets in code; production refuses
   test bypass tokens at startup.
