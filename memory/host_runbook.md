# Production host runbook (cPanel / VirtFS, /opt/stoic) — operator rules

Standing rules from the operator. Read before giving ANY host command.

## Before every deploy
1. Always `cd /opt/stoic` first — scripts are relative to the repo root.
2. **Check the latest backup exists before touching anything:**
   `ls -t backups/ | head -3`
   `update.sh` dumps Mongo automatically on each run (`backups/stoic-mongo-<ts>.archive.gz` + `.manifest.json`,
   14-day retention). A restore from the pre-update dump saved the day on 2026-10-02 — never deploy without
   confirming a fresh archive is present / was just written.
3. Full deploy: `STOIC_READINESS_POLICY=onboarding-close-only ./deploy/update.sh` while operator gates are pending;
   drop the flag once all gates are green.
4. **NEVER `git pull` in /opt/stoic before `update.sh`.** The script fetches + checks out `origin/main` itself and
   compares with the commit it started from; a prior pull makes it print "already on … — nothing to publish" and
   skip the build (happened 2026-10-03). Only `git fetch origin && git log --oneline -1 origin/main` to look.
   Recovery: `git checkout --detach <previous-commit>` then re-run `update.sh`.

## Never run raw docker compose on this host
- `docker compose up --force-recreate` → `overlay2 … device or resource busy` (VirtFS copies of /var/lib).
- Use `bash deploy/restart.sh <service>` (recreates through the reaper) after `backend/.env` edits,
  or `./deploy/update.sh` for a full deploy.
- "26 leaked copies could not be detached … not mounted" is benign noise (already gone when the sweep got there).

## Readiness / diagnostics
- Endpoint needs the metrics token:
  `curl -sS -H "X-Metrics-Token: $(. deploy/lib.sh; metrics_token)" http://127.0.0.1:8001/api/ops/release-readiness`
- Mongo access from a container (secrets are `*_FILE`): `docker compose exec backend python - <<'EOF' … EOF`
  with `url = os.environ.get("MONGO_URL") or open(os.environ["MONGO_URL_FILE"]).read().strip()`.

## Host-only .env state (NOT in repo)
- `INVENTORY_APPROVAL_MODE=single_admin` (set 2026-10-02; operator refuses a second admin)
- `LLM_MODEL_FAST=claude-haiku-4-5-20251001` (pending — operator asked for it; PR #17 LLM refactor lives on GitHub)
- `ATTESTATION_REQUIRED=false` still present on a PRODUCTION host — remove once release attestation exists.
- `ADMIN_MFA_ENFORCED`: do NOT change; admin already has TOTP.

## GitHub / workspace sync
- Workspace has no git remote; cannot pull. Operator develops on GitHub too (PR #17 = llm_client.py / llm_models.py /
  migrations/hash_bridge_tokens.py — later reverted; main == b66bf516 + one .gitleaksignore line, mirrored here 2026-10-02).
- Never "Force Push" from Save to GitHub; use "Create Branch & Push" on conflict.
- Content comparison trick: `git ls-tree -r <ref> -- <dir> | sort | sha256sum` on both sides.

## EA
- Demo MT5 terminals must run EA v1.58 (download from Accounts → compile in MetaEditor → attach); otherwise CLOSE_ONLY.
