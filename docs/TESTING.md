# STOIC Testing — clean-environment reproducibility (P0.1)

## Physical unit suite (no DB, no network, no backend)
The unit suite is PHYSICALLY scoped to `backend/tests/unit` and must pass
from a documented clean environment with no import-path magic:

```bash
python3.11 -m venv /tmp/stoic-venv && source /tmp/stoic-venv/bin/activate
grep -v '^emergentintegrations' backend/requirements.txt > /tmp/reqs.txt
pip install -r /tmp/reqs.txt --extra-index-url https://download.pytorch.org/whl/cpu
pip install --no-deps emergentintegrations==0.2.0 \
  --extra-index-url https://d33sy5i8bnduwe.cloudfront.net/simple/
cd backend
python -m pytest tests/unit -q     # NO env vars required (no MONGO_URL)
```

Import resolution comes from `backend/pytest.ini` (`pythonpath = .`) —
tests never mutate `sys.path`. `tests/unit/test_unit_independence.py`
enforces that no unit test touches a live database.

## Suite lanes (markers in backend/pytest.ini)
| lane        | needs                         | command                                   |
|-------------|-------------------------------|-------------------------------------------|
| unit        | nothing                       | `pytest tests/unit -q`                     |
| integration | MongoDB (`MONGO_URL`,`DB_NAME`)| `pytest tests/integration -m integration`  |
| http        | running backend (`REACT_APP_BACKEND_URL`) | `pytest tests -m http`         |
| broker/external/chaos/soak | live services   | on demand only, never ordinary CI          |

## Full dependency-backed suite (P0.3)
`scripts/run_full_suite.sh` runs the whole tree against the pinned
`backend/requirements.txt` deps and a real MongoDB. Redis is pinned in
requirements but the backend has no hard runtime dependency on it.

## Production-proof gates in CI (.github/workflows/ci.yml)
- `backend-unit` — physically isolated unit lane (`pytest tests/unit`).
- `backend-integration` — pinned deps + `mongo:7` service container.
- `clean-deploy-cert` — boots the committed backend on a fresh machine,
  proves liveness/readiness, then runs the v62.7 HTTP hardening suite
  (`tests/test_iter222_hardening_http.py`): RISK_UNKNOWN → BUY blocked,
  CLOSE/REDUCE allowed, through the application path (P0.2).
- `frontend-build` + `frontend-quality` — lint, typecheck, unit, build,
  E2E on the release commit (P0.4).
- `ea-compile` — REAL Windows MetaEditor compile of the frozen EA,
  fails on any MQL error, publishes the verified .ex5 (P0.5).

## Reproducible lanes & safety gates (audit items 41–45)
- **`make test-unit`** (or `scripts/test_unit.sh`) — validates Python 3.11 and
  every module the unit lane needs (incl. `bson`/`pymongo`, pulled in
  transitively), installs anything missing from `requirements.txt`, asserts
  the collected count matches `docs/TEST_MANIFEST.md`, then runs the lane.
  No tribal knowledge required.
- **RC dependency lock** — `make lock` writes `release/rc_lock.json`
  (Python/pip pins + hash, Node/yarn.lock hash, MongoDB, Playwright, plus
  Windows/MetaEditor/MT5 fields attested by the compile campaign);
  `make lock-check` fails on any drift. "It passed on our CI machine" is
  no longer an explanation.
- **Live-test safety** — every http-live test is DEMO_MUTATING by default:
  it requires `STOIC_ALLOW_MUTATING_TESTS=YES` and is FORBIDDEN when
  `APP_ENV` is production (or `STOIC_ENVIRONMENT=live`) unless the test
  carries `@pytest.mark.live_authorized`. Mark pure-GET tests
  `@pytest.mark.read_only` to exempt them. Categories: `read_only`,
  `demo_mutating` (default), `staging_mutating`, `chaos`, `destructive`.
- **Test identities** — credentials come from env (`ADMIN_EMAIL`/
  `ADMIN_PASSWORD` via `backend/.env`, injected as secrets in CI). The
  backend refuses login for disposable test-identity patterns
  (`@example.com`, `@test.*`, `TEST_`/`nonadmin_` prefixes, `+test@`) when
  `APP_ENV` is production — no test identity can authenticate to funded
  production (`test_identity.py`, configurable via
  `TEST_IDENTITY_BLOCK_PATTERNS`).
- **MQL5 verification chain** — mandatory before Demo Production Proof:
  exact MQ5 → Windows MetaEditor → 0 errors → EX5 → SHA-256 → Ed25519
  signature → `docs/RELEASE_HASHES.json`. Record with
  `scripts/verify_ea_release.py --ex5 … --compile-log … --sign`; verify with
  `make verify-ea`. `/api/ops/release-readiness` reports the chain always
  and enforces it when `REQUIRE_EA_RELEASE_PROOF=true` or in production.
