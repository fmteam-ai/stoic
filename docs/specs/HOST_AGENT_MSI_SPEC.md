# SPEC — Host Agent as a Signed Windows Service + MSI (Priority #3)

Status: SPECIFICATION (implementation pending) · Owner: Platform · June 2026

## 1. Goal
Replace the ad-hoc PowerShell script (`docs/host-agent/stoic-host-agent.ps1`)
with a **code-signed Windows service** distributed as a **signed MSI**, so the
agent survives reboots, runs under a least-privilege account, cannot be
trivially tampered with, and can be updated through the existing signed
release-channel pipeline.

## 2. Non-Goals
- No change to the agent↔backend HTTP contract (`/api/infra/agent/*` stays).
- No auto-install of MT5 terminals (existing command flow already covers it).
- No Linux/macOS agent (Windows Server 2019+ only).

## 3. Architecture

### 3.1 Components
| Component | Tech | Purpose |
|---|---|---|
| `stoic-agent.exe` | .NET 8 self-contained Worker Service (or Go 1.23 + `golang.org/x/sys/windows/svc`) | The service binary: heartbeat, command poll/ack, discovery, deploy-status, hardening report |
| `stoic-agent-config.json` | `%ProgramData%\STOIC\agent\config.json` | base_url, agent_id, cert paths; ACL'd to service account + Administrators |
| Credential store | Windows DPAPI (machine scope) / CNG key container | agent_token + mTLS private key never on disk in plaintext |
| `stoic-agent.msi` | WiX Toolset v5 | Install/upgrade/uninstall, service registration, event-log source |
| Watchdog | Windows Service Recovery (built-in) | Restart on failure: 5s / 30s / 120s, reset after 24h |

### 3.2 Service identity & privileges
- Runs as a **virtual service account** `NT SERVICE\StoicAgent` (no password,
  auto-managed SID) — NOT LocalSystem.
- Grants: `SeServiceLogonRight`, read/write to `%ProgramData%\STOIC\agent`,
  read-only to MT5 terminal dirs, **no** interactive logon.
- Firewall: outbound 443 to the STOIC hostname only (MSI adds the rule).

### 3.3 State machine
```
INSTALLED → ENROLLING (bootstrap/enrollment code → agent_token)
          → CSR_SUBMITTED (POST /api/infra/agent/cert/enroll, key in CNG)
          → ACTIVE (heartbeat 60s, poll 30s, jittered ±10%)
          → DEGRADED (backend unreachable — exponential backoff, max 15 min)
          → REVOKED (401 mtls/token → stop trading ops, event-log CRITICAL, retry enroll)
```

## 4. Code signing & supply chain
1. **Binary signing**: Authenticode (EV cert or Azure Trusted Signing),
   timestamped (RFC 3161). CI signs `stoic-agent.exe` AND the MSI.
2. **Release binding**: the MSI's SHA-256 is added to the existing Ed25519
   release manifest (`release_channels.py`) as artifact kind
   `host-agent-msi`; the agent's self-updater re-verifies both Authenticode
   and the pinned-Ed25519 manifest before applying an update (same dual-check
   pattern the PS1 uses today, iter-171 #4).
3. **Update flow**: agent polls the release channel → downloads MSI to a
   temp dir → verifies → schedules `msiexec /i /qn` via a one-shot scheduled
   task running as SYSTEM → service restarts → `POST /agent/deploy-status`.
   Rollback: keep previous MSI cached; on 3 failed starts the recovery action
   runs `msiexec` with the cached version and reports `status=failure`.

## 5. MSI authoring (WiX v5)
- UpgradeCode fixed: `{B7E6D2A4-STOIC-HOST-AGENT}` (generate once, never change).
- MajorUpgrade: `AllowSameVersionUpgrades=no`, `Schedule=afterInstallInitialize`.
- Properties (admin, unattended install):
  `BASE_URL`, `ENROLLMENT_CODE` (short-lived, from the portal), optional `PROXY_URL`.
  `msiexec /i stoic-agent.msi /qn BASE_URL=https://app.stoicaibot.com ENROLLMENT_CODE=ENR-XXXX`
- Custom actions (deferred, no impersonation): create ProgramData dirs + ACLs,
  register event-log source `StoicAgent`, first-run enrollment kick.
- Uninstall: stops service, revokes local credentials (best-effort
  `POST /agent/self-revoke` — new endpoint, token-authenticated), leaves logs.

## 6. Observability
- Windows Event Log (`StoicAgent` source): enroll, cert rotation, update,
  command exec, auth failures.
- Local ring-buffer log `%ProgramData%\STOIC\agent\logs\agent-*.log`
  (14-day retention), redaction of tokens.
- Heartbeat payload gains `service_version`, `msi_product_version`,
  `signed_binary: true`.

## 7. Security requirements (must-pass before GA)
- [ ] Binary + MSI Authenticode-signed and timestamp-verified in CI.
- [ ] agent_token & mTLS key stored via DPAPI/CNG only (no plaintext file).
- [ ] Service refuses to run if its own binary signature is invalid.
- [ ] Config file ACL: SYSTEM + Administrators + NT SERVICE\StoicAgent only.
- [ ] All existing PS1 behaviors covered by integration tests against a
      staging backend (register → enroll cert → heartbeat → command → update).

## 8. Migration plan
1. Ship MSI as **opt-in** alongside PS1 (portal shows both, MSI recommended).
2. Backend flags PS1 agents via `facts.agent_kind`; ops dashboard shows mix.
3. After 30 days, PS1 bootstrap endpoint returns a deprecation banner;
   after 60 days, new PS1 registrations disabled (existing keep working).

## 9. Effort estimate
| Work item | Est. |
|---|---|
| Worker-service port of PS1 logic | 4–6 d |
| DPAPI/CNG credential + CSR flow | 2 d |
| WiX MSI + custom actions | 3 d |
| CI signing pipeline (Trusted Signing) | 2 d |
| Self-update + rollback | 3 d |
| Staging integration tests | 2 d |
