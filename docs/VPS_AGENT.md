# STOIC VPS Agent — per-account portable MT5 terminals (Easy-Connect Phase 2)

One Windows VPS runs **one STOIC VPS Agent** (scheduled task `StoicVpsAgent`) and **one portable MT5
terminal per trading account** under `C:\STOIC\MT5\account-<login>\`. Installs are triggered from the
dashboard; the agent keeps every terminal alive.

## Prepare the VPS once (~5 min)

1. Install the broker's MT5 normally, copy its program folder to `C:\STOIC\golden\MT5`.
2. `C:\STOIC\golden\MT5\terminal64.exe /portable` → log in to any demo account → Tools → Options →
   Expert Advisors → tick *Allow WebRequest for listed URL* → add `https://<your-stoic-host>` → OK → close MT5.
   The allow-list is part of the golden folder and is cloned into every account terminal (the spike's attempt B
   model; if a broker build does not honour the copied config, Install Progress shows the one click to make).
3. Dashboard → VPS → *Connect existing VPS* → copy the enrollment code. In an **Administrator PowerShell**:
   ```powershell
   irm https://<your-stoic-host>/api/setup/agent.ps1 | iex
   Install-StoicAgent -ServerUrl "https://<your-stoic-host>" -EnrollmentCode "<code>"
   ```
4. For every account you will install, type its MT5 password **once, on the VPS** (DPAPI-encrypted under
   `%ProgramData%\Stoic\logins\`, never transmitted):
   ```powershell
   Set-StoicTerminalLogin -Login 12345678 -Server "Broker-Demo"
   ```
   Windows auto-logon for the VPS user is recommended: the task runs at logon so the terminals stay visible over RDP.

## Install an account

Dashboard → Accounts → account → **Quick Install → INSTALL ON MY VPS** (`POST /api/vps/agents/{agent_id}/install-terminal`).
The server issues a fresh single-use pairing code and queues a signed `install_terminal` command. Within 30 s the agent:

1. clones the golden folder (robocopy, without `logs\` and `config\accounts.dat`);
2. downloads the hash-pinned public installer and runs `Install-Stoic -TerminalPath <clone> -NoRestart -TerminalLogin <login>`
   (EA, bridge token, `STOIC-Server.txt`, `stoic-start.ini`) — the same path a manual install takes;
3. starts `terminal64.exe /portable /config:stoic-first-start.ini` with `[Login]` from the DPAPI store, then deletes that ini;
   without a stored password the terminal starts and reports **awaiting_login**;
4. reports `installing → running | awaiting_login | failed` to `POST /api/vps/agent/terminals/report`
   (mirrored as `accounts.vps_terminal`, shown in Install Progress).

## Watchdog (every 30 s)

`POST /api/vps/agent/terminals/status` tells the agent, per terminal, whether the EA heartbeat is fresh.
Process dead → start. Heartbeat older than **180 s** (and the terminal is `running`/`restarted`) → `CloseMainWindow`
+ 60 s wait, then start again. At most **3 restarts per hour** per terminal; then status `restart_loop` and the
server raises the critical ops alert `vps_terminal_restart_loop` (mutable like every evaluator kind). MT5 is
never force-killed; a terminal `awaiting_login` is never restarted.

## Files on the VPS

| Path | Content |
|---|---|
| `%ProgramData%\Stoic\vps-agent.json` | server URL, agent id, DPAPI-protected agent token + command key |
| `%ProgramData%\Stoic\logins\<login>.json` | DPAPI-protected MT5 password per login |
| `%ProgramData%\Stoic\vps-agent.log` | agent log |
| `C:\STOIC\golden\MT5` | golden portable terminal (prepared by you) |
| `C:\STOIC\MT5\account-<login>\` | one portable terminal per account |

Commands the agent honours: `install_terminal`, `restart_terminal {login}`, `run_diagnostics`
(signed sequence — HMAC with the enrolment `command_key`; anything else is acked as unsupported).
