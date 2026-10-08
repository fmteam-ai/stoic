# Security Audit #14 — 2026-10-08

Scope: everything shipped for the main113 review (N113-1…7, A17-13; VPS agent v1.2) with a sampled full-surface
recheck and bounded read-only black-box on www.stoicaibot.com, the preview and `stoic-signer.fly.dev`.

## Verdict: PASS with P3s — no P0–P2

| Sev | Finding | Status |
|---|---|---|
| P3 | `Assert-StoicTrustedOwner` / `Test-StoicLockedDown` compared `Get-Acl` owner **names** (`BUILTIN\Administrators`, `NT AUTHORITY\SYSTEM`) — on a localized Windows a legitimately owned folder is refused (fails CLOSED: refusal / `awaiting_login`). | Fixed (agent **v1.3**): well-known **SIDs** (`S-1-5-32-544`, `S-1-5-18`, current user SID) via `GetOwner([SecurityIdentifier])` / `NTAccount.Translate`; name compare only as fallback when translation fails. |
| P3 | Signer security-header drift (carry-over from #13). | Operator: `cd deploy/signer && flyctl deploy -a stoic-signer`. |
| INFO | Prod runs a build behind the audited HEAD (`/api/setup/agent.ps1` 404 on prod, 200 + BOM + ASCII + `X-STOIC-SHA256` on preview). | Ship the audited build (`update.sh`) before relying on the VPS flow. |

## Verified safe-by-design (explicit false-positive calls)
- Paper free-form `account_number`: typed `str` (no Mongo `$`-operators), never reaches a VPS/shell/ini/path — `queue_install_terminal` refuses paper and re-validates the login (4–12 digits), the agent validates again; `PATCH /accounts/{id}` cannot flip `mode`, so a paper id cannot become a live login.
- `declared_environment` is only an extra refusal gate (403 `real_refused_demo_policy`) + a stored record — never read as trading authority (`broker_env.attested_environment`, `trading_enabled=False` default, EX5 proof decide). `algo_trading_consent` is a recorded consent artifact, not a server-side execution gate — acceptable, trade execution is independently gated.
- Command-seq resync: `$max`-bounded (0 < n ≤ 2^53), never rewinds, scoped to the authenticated agent's own document; `ack_command` looks commands up by `agent_id`, so a replay ack cannot touch another agent's command; no overflow / seq collision; the agent's `last_seq` check still blocks true replays after a resync.
- `Password=` allowing `= [ ]`: CR/LF/NUL refused, so one line cannot inject an ini section/key; value is operator-typed and DPAPI-local.
- CI Windows PowerShell 5.1 parse gate loads the agent without `-Run` (loop guarded), `permissions: contents: read`, no secrets.
- Updated integration tests (main101/102/104, r18 `capital_capable` guard) weakened no security assertion.

## Black-box (read-only)
401 on unauth `/api/vps/*`, `/api/ops/*`, `/api/accounts/broker-presets`; `/api/infra/agent/commands/ack` with bogus token + `last_seq` → 401, no stack trace; CORS `*` only on non-credentialed routes (no `Allow-Credentials`); agent.ps1 integrity header correct on preview.

## Tests
`tests/unit/test_security_audit13.py` (+1, SID contract). Unit lane green; manifest + rc_lock regenerated.
