# Security Audit #23 — 12 Oct production fixes + Item 4 demo period

Scope: every surface changed since audit #22 (`docs/SECURITY_AUDIT_22.md`): rules-policy upload accepting a raw
`policy_text` file (`routes/authority_routes.py`, `rules_policy.body/problems/activate/load_latest`, renewal
`repo_configured`); `deploy/nginx.conf` variable upstream (`resolver 127.0.0.11` + `set $backend` + `proxy_pass $backend`),
`deploy/lib.sh wait_api_health_via_frontend`, `deploy/restart.sh`; `scripts/verify_ea_release.py load_backend_env_pins`
(three key pins read from `backend/.env`), `deploy/bootstrap.sh` attestation flag handling + `lib.sh attestation_required()`;
`backend/ops/prepromotion_evidence.py` demo-period regime (`regime_from_policy` / `environment_gates`);
`.github/workflows/model-manifest-resign.yml` (lock + summary re-bind) and the v2-signed `MODEL_MANIFEST.json`; standard
perimeter sweep. Public GET probes only against production (`/api/health` v1.60.21, attested) and preview; no login and
no mutating request to production. Audit #22 accepted-by-design items, #21 P3 notes and PRD "IGNORE IT" false positives
were not re-reported.

**Verdict: PASS — no P0/P1/P2. Three P3 hardening notes; two fixed in this release, one accepted.**

| ID | Sev | Location | Finding | Status |
|---|---|---|---|---|
| SEC-023-1 | P3 | `routes/authority_routes.py` `rules_policy_upload` | 422 for an unparsable `policy_text` echoed the raw `json.loads` exception text (admin + step-up only). | FIXED — generic message ("Not valid JSON — paste the signed policy file exactly as written by the signer"). |
| SEC-023-2 | P3 | `deploy/nginx.conf` `/api/` | The raw-text branch is capped at 64 KiB (413 before parsing) but the legacy dict path relied on nginx's implicit 1 MB default; no explicit `client_max_body_size` at the edge (app-level `RequestBodyLimit` 4 MiB exists). | FIXED — explicit `client_max_body_size 1m;` in `/api/` (no file uploads exist). |
| SEC-023-3 | P3 | `rules_policy.load_latest`, `GET /authority/policy/renewal` | `repo_configured` / `note` ("GITHUB_REPO not configured …") disclose deployment configuration state — admin (role + 2FA) only, no secret value. | ACCEPTED — operator guidance explicitly requested by the 12 Oct production report. |

## Confirmed safe
- Policy upload: `require_step_up` + `_admin` run before any parsing; 64 KiB / 413 bound; `json.loads` float-preserving;
  Ed25519 over the canonical body, installation id, environment, expiry and nonce-replay checks unchanged.
- nginx: `$backend` is a constant (no request input) → no SSRF / open proxy / host-header pivot; Host, X-Forwarded-For
  chain, upgrade and timeout headers preserved; resolver is Docker's embedded DNS on the compose network only.
- `verify_ea_release.py --check`: loads only the three pin names, never prints them, exported values win;
  `resolve_public_key` still enforces the committed `release/release_key.fingerprint`, so a repo-write actor cannot
  weaken the gate without tampering the in-repo trust root (unchanged model); `STOIC_BACKEND_ENV_FILE` only applies to
  the invoking shell's own environment.
- bootstrap: `ATTESTATION_REQUIRED=false` is exported for the bootstrap process only on public hosts and the `.env` line
  is stripped; `attestation_required()` defaults to REQUIRED in production and fails closed on unknown values.
- Demo period: `attested_environment` is server-authoritative (admin attestation agreeing with the bound identity; a
  broker-reported real trade mode voids it); a LIVE account under `demo_only` fails `all_enabled_attested_demo`; an
  expired policy falls back to the LIVE regime; the signed bundle records `regime` + `evidence_label`; only the LIVE-only
  performance attestation is skipped.
- model-manifest-resign: dispatch-only, secrets via env (no `${{ }}` shell interpolation), plain push; the repo
  `MODEL_MANIFEST.json` carries `key_id stoic-release-ed25519-v2` whose fingerprint matches the committed `current` line.
- Perimeter: `secret_scan.py --history` 0 blocking; CORS allowlist + credential strip, httpOnly/Secure/SameSite cookies,
  CSRF double-submit unchanged; pins `pymongo 4.18.2` / `multidict 6.9.1` carry no reachable advisory.

## Not exercised
Authenticated flows were verified from source and from the preview unit/HTTP suites, not with the auditor's own
privileged session. No mutating request was sent to production.
