# Security Audit #33 — v1.60.41 diff + regression sweep

**Verdict: PASS — no P0–P2.** Scope: today's changes (audit-chain concurrency + re-anchor, EA previous-record
chain and fleet-compat gate, rollback/update tree reset, CI signing steps) plus a sweep of auth, `/api/ops`,
bridge token handling, CORS, secrets.

## Verified invariants
- `audit_chain.repair_chain` / `plan_relink` refuse any entry whose content no longer matches its own hash; only
  links/sequence are re-written, originals snapshotted to `audit_chain_repairs`, the repair is a chained marker.
  Reachable only from `scripts/reanchor_audit_chain.py` (operator shell, MONGO_URL) — no HTTP route.
- `ea_capabilities` accepts a `previous` EX5 only when (a) the current record's Ed25519 signature covers
  `previous.{version, ex5_sha256}` and (b) `previous` verifies on its own accepted-key signature. The authoring
  scripts (`verify_ea_release.resolve_previous`, `capture_release_hashes.merge_ea_entry`, `check_ea_fleet_compat`)
  cannot promote an unsigned/tampered hash.
- `deploy/rollback.sh` / `update.sh`: `git reset --hard` / `checkout` touch tracked files only; `.env*`, `secrets/`,
  `*.key` are git-ignored and untracked; `REF` always quoted (no injection from argv / `releases.log`).
- Workflows never print secrets; `RELEASE_PUBLIC_KEY_B64` is public; signer token passed via `--token-file`;
  `signer_probe.json` carries only `ok`/`error`/key ids.
- CORS: credentials only with an explicit allowlist; `/api/ops` = admin role + enrolled 2FA; bridge = hashed token +
  IP throttle + suspension; `secret_scan` 0 blocking.

## Hardening
- **P3 (applied)** — `pip install cryptography` was unpinned in `ci.yml`, `release.yml`, `ea-release.yml`,
  `model-manifest-resign.yml`; now `cryptography==50.0.0 requests==2.34.2 pymongo==4.18.2` (= `backend/requirements.txt`).
- **P3 (noted)** — keep `signer_probe.py` output free of token fields if its schema grows (`release_signing.signer_health`
  returns only `ok`, `error`, `remote_key_id`, `identity_matches`).

## Limits
Static review + git history; live DB/runtime not exercised. Unchanged surface at spot-check depth (audits 13–32 open P0–P2: none).
