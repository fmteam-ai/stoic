# Security Audit #15 — 2026-10-08 (scope: changes since audits #13/#14)

Scope: secrets_loader (blank env → unset, `*_FILE`), release_signing pin/verify, host_profile HMAC, trading_authority
deploy_posture / VPS_AGENT_DEGRADED, ops release-readiness, public `/api/status` release block, deploy_jam marker,
vps_agent heartbeat / agent_degraded, STOIC-Agent.ps1 SecureString + degraded state, AddAccountWizard / ReadinessCard /
StatusPage, deploy/preflight.sh + host-prereqs.sh + restart.sh + docker-root-slave.sh, verify_ea_release.py, CI workflows.

Verdict: **CONDITIONAL PASS → all findings fixed in this commit.**

| ID | Sev | Location | Finding | Fix |
|----|-----|----------|---------|-----|
| SEC-001 | P2 (MEDIUM, likely) | `.github/workflows/deploy-production.yml:53` | `github.event.workflow_run.head_branch` / `inputs.ref` expanded inline in `run:` before the sanitising regex — a crafted `v*` branch name could run commands in the job holding `DEPLOY_SSH_KEY`. | Bound to `env:` (`EVENT_NAME`, `HEAD_BRANCH`, `INPUT_REF`) and referenced as `"${VAR}"`. Guard test `test_audit15_no_inline_untrusted_context_in_workflow_run_steps` scans every workflow. |
| H-1 | P3 | `backend/host_profile.py` | `profile\|markers\|detected_at` joined with `\|` — a marker containing `\|` could shift fields (theoretical: markers are host-detected tokens). | Length-prefixed canonicalisation `len:value;` in `canonical_payload()`, mirrored in `deploy/preflight.sh`. |
| H-2 | P3 | `deploy/preflight.sh` | Trust-on-first-use when no committed fingerprint existed for the key id. | Removed: the preflight refuses to pin any key id without a fingerprint in `release/release_key.fingerprint` (test `test_audit15_release_key_pin_refuses_tofu`). |
| H-3 | P3 (info) | `/api/status` | Public page shows short commit, region, app_env. | Accepted — intentional status copy, no paths/identifiers. |

Verified-OK controls (no change): `drop_blank_env` fails closed (`METRICS_TOKEN` empty → 503, constant-time compare);
`verify_hex`/`public_key_b64` refuse release purposes without the pin outside `local` mode; `deploy_jam` settable only by
the in-container CLI and enforced fail-closed at the choke point (`deploy_posture_domain`, `enforce_new_trade`);
all `/api/ops/*` gated by metrics token or admin-2FA (+ step-up on mutations); agent heartbeat/`report_terminal` require the
hashed 256-bit agent token and scope status + alert dedup to the agent's own user; PS1 `Invoke-Expression` gated by the
signed-parameter SHA-256 pin + https-only `server_url`; MT5 passwords stay DPAPI/SecureString, never transmitted;
`set_kv` safe for base64/hex values; CI pins the external wheel by sha256.

Gaps: wider application (auth, payments, BOLA on unchanged routes) not re-reviewed; no runtime black-box.
