# SPEC — Split Backend Dependency Set into Purpose-Built Images (Priority #7)

Status: SPECIFICATION (implementation pending) · Owner: Platform · June 2026

## 1. Problem
The single backend image installs the FULL `requirements.txt` (~200+ packages:
FastAPI + pandas/numpy/scikit + research/genetics + report tooling). Impact:
- Large attack surface on the internet-facing API (research libs with CVEs
  ship in the same container that terminates user traffic).
- Slow cold starts / large pulls (multi-GB image), slow CI rebuilds.
- One vulnerable transitive dep forces redeploying everything.

## 2. Target topology — 4 images from one repo
| Image | Serves | Entrypoint | Dep profile |
|---|---|---|---|
| `stoic-api` | All user/admin HTTP routes | `uvicorn server:app` | `requirements/api.txt` — fastapi, uvicorn, motor/pymongo, pydantic, cryptography, pyjwt/passlib, stripe, httpx/requests, pyotp, emergentintegrations |
| `stoic-worker` | Background loops: heartbeat policies, auto-rollback watcher, audit anchoring, alert fanout, subscription sweeps | `python -m workers.main` | api.txt + scheduling (no ML) |
| `stoic-research` | Strategy research, genetics/optimizer, twin/backtests, stress tests | `python -m research.main` (queue consumer) | api.txt + numpy, pandas, scikit-learn, scipy, statsmodels |
| `stoic-maintenance` | One-shot jobs: index-manifest apply, migrations, DR drills, seed | `python -m maintenance.cli <job>` (Job/CronJob) | api.txt + backup/restore tooling |

## 3. Dependency layout (pip-tools)
```
backend/requirements/
  base.in      # shared: pymongo/motor, pydantic, cryptography, dotenv
  api.in       # -r base.in + web stack
  worker.in    # -r api.in  (workers import route-layer services today)
  research.in  # -r base.in + scientific stack
  maint.in     # -r base.in + ops tooling
```
- CI runs `pip-compile --generate-hashes` per profile → four locked
  `*.txt` files; the legacy top-level `requirements.txt` becomes the UNION
  (kept temporarily for the preview/dev container and pytest).
- Guardrail test: `tests/test_dep_profiles.py` asserts the API profile does
  NOT contain numpy/pandas/scikit and that every `import` reachable from
  `server.py` resolves inside `api.txt` (walk `modulegraph` or a curated
  denylist grep in CI).

## 4. Code boundaries required before the split
1. **Lazy-import audit**: research-only modules (`genetics_*`, `twin_*`,
   `stress_*`, `research_*`) must not be imported at `server.py` startup.
   Route handlers that need them enqueue a job instead (see §5).
2. **workers/** package: move the current in-process asyncio background tasks
   (started in server lifespan) behind `RUN_BACKGROUND=api|worker|off` env so
   the API image runs with `off` and the worker image owns them exclusively.
   (Distributed lock via Mongo `findAndModify` lease per loop — prevents
   double-running during the transition.)
3. **maintenance/cli.py**: wraps existing scripts (`generate_index_manifest`,
   seeders, DR drill) with one argparse entrypoint.

## 5. Inter-service contract
- Queue: MongoDB collection `jobs` (existing pattern) — API inserts
  `{kind, payload, status:queued, lease_until}`; research/worker consumers
  claim with atomic `find_one_and_update`. No new broker (Redis optional
  later, aligns with Priority #6).
- All four images share ONE git SHA and ONE release manifest entry; the
  Ed25519-signed manifest lists each image digest
  (`ghcr.io/stoic/api@sha256:…`) so promotion/rollback moves them in lockstep.

## 6. Dockerfiles (multi-stage, one file, four targets)
```dockerfile
FROM python:3.11-slim AS base          # non-root user, tini, CA certs
FROM base AS api
COPY requirements/api.txt .
RUN pip install --no-cache-dir --require-hashes -r api.txt
COPY backend/ /srv/app
USER 10001
CMD ["uvicorn","server:app","--host","0.0.0.0","--port","8001"]
# targets: worker / research / maintenance — same pattern, own txt + CMD
```
Build: `docker build --target api -t stoic-api .` (CI matrix over targets,
layer cache keyed on the profile txt hash).

## 7. Runtime hardening deltas
- API image: `read_only: true` rootfs, drop ALL caps, no build tools.
- Research image: CPU/mem limits (it hosts the heavy libs), NO inbound port,
  egress limited to Mongo + market-data hosts.
- Maintenance: run as Kubernetes `Job` with a dedicated Mongo role that is
  the ONLY one allowed `createIndex`/migrations (API user loses DDL rights).

## 8. Rollout plan
1. Phase 0: land `requirements/` profiles + guardrail test (no image change).
2. Phase 1: introduce `RUN_BACKGROUND` env split; deploy worker image next to
   the monolith (loops leased — safe overlap).
3. Phase 2: move research routes to queue-based execution; deploy research
   image; API image rebuilt WITHOUT scientific stack.
4. Phase 3: maintenance Job image; revoke DDL from the API DB user.
5. Phase 4: delete the union `requirements.txt` from production builds
   (preview/dev keeps it for the all-in-one container).

## 9. Acceptance criteria
- [ ] API image < 350 MB; contains no numpy/pandas/scikit/scipy.
- [ ] `pip-compile --require-hashes` lockfiles for all four profiles in CI.
- [ ] Background loops run exactly-once across API+worker (lease test).
- [ ] Release manifest pins all four image digests; rollback moves all four.
- [ ] Full pytest suite green against the split preview compose file.

## 10. Effort estimate
| Work item | Est. |
|---|---|
| Dep profiling + pip-tools + guardrail test | 2 d |
| Background-loop extraction w/ leases | 3 d |
| Research queue consumers | 3–4 d |
| Dockerfiles + CI matrix + signing | 2 d |
| Maintenance CLI + DB role split | 2 d |
