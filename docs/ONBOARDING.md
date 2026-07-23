# STOIC — Developer Onboarding

## Stack
React 19 (CRA/CRACO, Tailwind, shadcn/ui, recharts) · FastAPI (async, Motor)
· MongoDB · MQL5 EA · pytest (2800+ tests).

## Local development
Supervisor runs both services with hot reload:
```bash
sudo supervisorctl status                  # backend :8001, frontend :3000
sudo supervisorctl restart backend         # only after .env / dependency changes
tail -n 100 /var/log/supervisor/backend.err.log
```
- Backend deps: `pip install <pkg> && pip freeze > backend/requirements.txt`
- Frontend deps: `cd frontend && yarn add <pkg>` (never npm)

## Non-negotiable conventions
1. **Every API route is prefixed `/api`** (ingress routes it to :8001).
2. **Frontend always calls `process.env.REACT_APP_BACKEND_URL`** — never a
   hardcoded URL. Backend reads `MONGO_URL`/`DB_NAME` from env only.
3. **Never return raw Mongo documents** — ObjectId isn't JSON serializable.
   Convert to `str` / use the model helpers.
4. **datetime**: `datetime.now(timezone.utc)`, ISO strings in Mongo.
5. **Interactive UI elements need `data-testid`** (kebab-case) — the test
   agent drives the UI through them.
6. **Tests use repo-relative paths** (`Path(__file__)`), never `/app/...`.
7. Auth flows (login/register/reset/2FA/step-up) follow the integration
   playbooks — don't hand-roll crypto.

## Test suite
```bash
cd backend
python -m pytest tests/unit -q                  # fast, no server needed
python -m pytest tests/test_iter153_step_up.py -q   # HTTP tests hit the live preview URL
```
- HTTP tests read the base URL from `frontend/.env` and auto-attach
  `X-RateLimit-Bypass` + `X-Step-Up-Bypass` (conftest). A test that wants the
  REAL step-up gate sets `session.headers["X-Step-Up-Bypass"] = ""`.
- Test credentials live in `memory/test_credentials.md`. Never enroll 2FA on
  `admin@trading.bot`. New passwords must be strong — HIBP screening rejects
  `password123`-class fixtures.

## Where things live
| Area | Files |
|---|---|
| Auth + MFA + step-up | `backend/routes/auth_routes.py`, `backend/step_up.py`, `backend/totp.py`, `backend/hibp.py` |
| Bot engine + safety | `backend/routes/bot_routes.py`, `backend/bot_runner.py`, `backend/position_protector.py` |
| Scalp fast path | `backend/scalp/` (engine, gates, order_state, risk_reservations, outbox) |
| MT5 bridge | `backend/routes/bridge_routes.py`, `backend/static/EmergentTradingBridge.mq5` |
| Workers | `backend/workers/` (leader-lease pattern in `base.py`) |
| Verified performance | `backend/routes/performance_routes.py`, `frontend/src/pages/VerifiedPerformance.jsx` |
| Frontend API layer | `frontend/src/lib/api.js` (CSRF, silent refresh, step-up interceptor) |
| App shell + nav | `frontend/src/components/AppLayout.jsx`, `Sidebar.jsx`, `App.js` |

## EA development
- Source: `backend/static/EmergentTradingBridge.mq5` (v1.53). Bump
  `#property version` AND the structural checker expectations together —
  `python scripts/check_ea_structure.py` and the CI `ea-compile` job gate it.
- Core invariants: durable intent journal before OrderSend, sequence fencing,
  deal-level netting truth, Unresolved-Accepted keeps risk reservations.

## Reading order for a new engineer
1. `docs/ARCHITECTURE.md` — diagrams + invariants
2. `memory/PRD.md` — full change history by iteration
3. `backend/scalp/engine.py` — the most concentrated business logic
4. `docs/API.md` — endpoint map
5. `docs/RUNBOOK.md` + `docs/MT5_VALIDATION_CAMPAIGN.md` — operations
