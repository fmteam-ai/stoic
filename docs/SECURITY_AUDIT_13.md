# Security Audit #13 — 2026-10-08

Scope: full application (FastAPI backend, auth/MFA, secrets vault, VPS Agent Service endpoints +
`STOIC-Agent.ps1` v1.1, frontend, CI/release signing), with line-by-line focus on everything shipped
since Audit #12 (commit `d58c2aa7` → `4f7be475`: main112 review + A17 fix list). Black-box, read-only
sampling on www.stoicaibot.com, the preview and `stoic-signer.fly.dev`.

## Verdict: PASS with P3s — no P0–P2

| ID | Sev | Finding | Status |
|---|---|---|---|
| SEC-001 | P3 | Live signer `GET /public-key` returns none of the audit-#12 headers (HSTS, `X-Content-Type-Options`, `Cache-Control: no-store`) although `deploy/signer/app.py:81-88` adds them → the hardening is in source but **not deployed**. Integrity controls (`purpose` required, 401/403 on `/sign`) ARE live. | Code: `scripts/signer_probe.py` now emits `warnings` on header drift / `Server` banner; `ea-release.yml` surfaces them as `::warning::` (advisory, never blocks). **Operator:** `cd deploy/signer && flyctl deploy -a stoic-signer` (code only — never `init_fly_signer.sh`). |
| INFO | P3 | Agent-reported `started_at` was stored verbatim → a compromised agent could post a far-future value and hold its own restart grace open forever (tenant-scoped, no cross-tenant effect). | Fixed: `vps_terminals.report_terminal` clamps `started_at` to the server receipt time. |
| INFO | P3 | `X-Client-Cert-Fingerprint` is a pinned secondary identifier, not cryptographic mTLS (`routes/infra_routes.py`). | Accepted (unchanged since earlier audits; the hashed bearer token is the credential). |

## Audit #12 hardenings — verified holding (source trace)
- `sig_v2` required by the agent on every executed command; HMAC compared constant-time; `last_seq` replay check; fail-closed without `command_key_enc`; one command per poll.
- Login forced to `^\d{4,12}$` at three layers (AccountCreate validator, `queue_install_terminal`, agent `Assert-StoicLogin`); the agent derives `C:\STOIC\MT5\account-<login>` itself and ignores any server `directory` → no path traversal / UNC / ADS.
- `installer_sha256` is taken from the **signed** params, never from the response header.
- 409 `terminal_exists` is not an oracle: ownership (`LookupError`) is evaluated before the live-terminal check (`PermissionError`); `replace=true` cannot target another user's account.
- `report_terminal`: types validated (422), account ownership enforced on both the `mt5_instances` row and the `accounts.vps_terminal` mirror (user-scoped update filter); invalid ObjectId → 404.
- Install/restart rate limits keyed per user; `/api/infra/agent/register` 20/10 min per IP; mute endpoints behind TOTP step-up.
- PowerShell: every server-controlled string that reaches an ini / robocopy / Start-Process argument passes `Assert-StoicIniValue` (no `\r \n \0 [ ] =`), symbol/login regexes; DPAPI files under `C:\STOIC` with inheritance removed.

## Black-box (read-only)
- Unauth `/api/vps/*`, `/api/ops/alerts*` → 401/403, never 500; static error bodies, no stack traces.
- `/api/setup/agent.ps1` serves `X-STOIC-SHA256`; CORS does not reflect hostile origins on credentialed routes; Turnstile on login.
- Prod ran `69bd715c`, preview `b3b7eb65` — both behind reviewed HEAD, so A17 behaviour was inferred from source and the unit lane.

## Still accepted as-is (from earlier audits)
Wildcard CORS on non-credentialed public endpoints · pairing tokens stored unhashed (192-bit, single-use,
atomic consume) · `/api/health` metadata unauthenticated (deploy gate reads `build_sha`) · bot token
decryptable by API + workers via the vault overlay · installer parameters operator-local.

## Tests
`backend/tests/unit/test_security_audit13.py` (2). Unit lane green; `docs/TEST_MANIFEST.md` + `release/rc_lock.json` regenerated.
