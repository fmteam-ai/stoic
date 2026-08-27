# STOIC Host Agent

The Host Agent ships as a **signed Windows MSI** built by
`.github/workflows/msi-release.yml`.

Contents installed to `C:\Program Files\STOIC Host Agent`:

| File | Purpose |
|------|---------|
| `EmergentTradingBridge.mq5` | The STOIC bridge EA (attach inside MT5) |
| `install_host_agent.ps1` | Bootstrap: copies the EA into every MT5 terminal + registers a daily NTP clock-resync task |
| `README.md` | This file |

## Build, sign & verify

Trigger the **MSI Release** workflow (manual dispatch or a
`host-agent-v*` tag). The pipeline:

1. Builds `StoicHostAgent.msi` with WiX v5 on `windows-latest`.
2. **Signs** it with `signtool` when the repository secrets
   `CODESIGN_PFX_BASE64` + `CODESIGN_PFX_PASSWORD` are configured
   (an OV/EV code-signing certificate exported as base64 PFX).
   Without the secrets the MSI is produced **UNSIGNED** and the job
   summary says so loudly.
3. Runs `signtool verify /pa` and writes a `SHA256SUMS.txt` manifest.
4. A **separate job on a fresh runner** downloads the artifact and
   independently re-verifies the Authenticode signature and the hash —
   the build job never verifies itself alone.

## Clock health

The bootstrap registers a daily `w32tm /resync` task. The EA reports
`client_time_ms` (GMT epoch ms) on every heartbeat; the backend computes
the skew and surfaces it at `/api/latency/clock-skew` and in
`/api/brain/health?scope=account`.
