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
3. Dashboard → VPS → *Connect existing VPS* → **copy the one-line enrol command shown there** (it pins the agent
   script's SHA-256 and passes it on as `-ExpectedSha256`, so the copy the logon task runs is verified too) and paste it
   into an **Administrator PowerShell**. Never use a plain `irm | iex` without the pin.
   `Install-StoicAgent` also sets an explicit ACL on `C:\STOIC` (Administrators, SYSTEM, the agent user; inheritance off).
3b. **Required: Windows auto-logon** for the agent user (`netplwiz` → untick "Users must enter a user name and password",
   or Sysinternals `Autologon.exe`). The task runs at logon; after a reboot without auto-logon nothing runs — the
   dashboard raises the critical `vps_agent_offline` alert after 5 minutes of silence.
4. For every account you will install, type its MT5 password **once, on the VPS** (DPAPI-encrypted under
   `%ProgramData%\Stoic\logins\`, never transmitted):
   ```powershell
   Set-StoicTerminalLogin -Login 12345678 -Server "Broker-Demo"
   ```
   The password is checked when you type it (N113-4): `= [ ]` and other symbols are fine (MT5 reads everything after
   the first `=`); only line breaks and **non-ASCII** characters are refused — MT5's startup ini is ASCII, so change
   such a password at the broker or log in by hand once. A stored password that later fails the check does not fail
   the install: the terminal starts without `[Login]` and reports `awaiting_login` with the reason.
   Windows auto-logon for the VPS user is recommended: the task runs at logon so the terminals stay visible over RDP.

Both `C:\STOIC` and `%ProgramData%\Stoic` are locked down with `icacls /inheritance:r` (Administrators, SYSTEM and the
agent user only, applied to the whole tree) **before** the first file is written (N113-3). The agent refuses to use a
folder or file another local user owns ("refusing to use ... owned by") — delete it as Administrator and re-run the
enrol line. An `icacls` failure stops the enrolment (no warning-and-continue).

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
+ 60 s wait, then start again — but never within **180 s after a start** (grace), and never while the terminal's own
Experts log advanced in the last 3 min (a heartbeat-ingest outage on the server must not restart every terminal).
At most **3 restarts per hour** per terminal (ledger persisted in `%ProgramData%\Stoic\restarts.json`, survives agent
restarts); then status `restart_loop` and the server raises the critical ops alert `vps_terminal_restart_loop`.
MT5 is never force-killed; `awaiting_login`, `installing`, `queued`, `stopped`, `failed`, `restart_loop` terminals are
never restarted (N113-5). A failing terminal entry is isolated (per-terminal try/catch) and never stops the cycle. The dashboard's
RESTART TERMINAL asks for confirmation (positions unmanaged during the restart). Installing over an account that
already has a live terminal is refused (`409 terminal_exists`) until you confirm the replacement.

## Recovery (N113-6)

- **Commands stuck as "delivered" after a server database restore**: the restored `command_seq` is behind the agent's
  `last_seq`, so the agent sees every new command as a replay. The agent acks such a command with `seq_replay` and
  reports `last_seq` in every heartbeat; the server advances its sequence (`$max`, never rewinds) — just queue the
  command again from the dashboard. No re-enrol needed.
- **`vps-agent.json` corrupted** (log: "agent not installed"): it is written atomically (temp file + `[IO.File]::Replace`),
  so this should not happen on a crash; if it does, re-run the enrol line from Dashboard → VPS → Connect existing VPS.
- **Agent task retries every minute without logging**: run
  `powershell.exe -NoProfile -File %ProgramData%\Stoic\STOIC-Agent.ps1` by hand to see the parse error. The served
  script is ASCII-only with a UTF-8 BOM and CI parses it under Windows PowerShell 5.1 (N113-2).
- **Agent `agent_degraded` / "restart ledger unsaved"** (P2-01): `%ProgramData%\Stoic\restarts.json` could not be written
  (ACL, disk full). The agent re-probes once per loop; the first save that reads back **byte-exact** (SHA-256 of the file
  vs the bytes written — M117-2, never a re-serialised JSON comparison) starts a 10-minute stability window
  (detail `recovering: …`); automatic restarts resume after it. Behavioural test: `scripts/test_agent_recovery.ps1`
  (CI runs it under Windows PowerShell 5.1).

## Crash dumps and secrets in memory (A19-P2-01 / A20-P2-01)

While running, the agent holds the DPAPI-unprotected agent token and — only during a terminal start — the MT5
password as managed .NET strings. A user-mode crash dump of `powershell.exe` or `terminal64.exe` could contain them.

- The agent itself only sets the **narrow, per-user** `HKCU\…\Windows Error Reporting\LocalDumps\DumpCount=0`
  (best effort — Windows reads LocalDumps primarily from HKLM, so treat it as a hint, not a guarantee). Agent 1.3
  also set the user-wide `Windows Error Reporting\Disabled=1`, which switched error reporting off for *every*
  program of that user; **1.4 no longer sets it and removes it on upgrade**.
- **Recommended: run the agent under its own Windows user** (e.g. `stoic-agent`, local, auto-logon, member of
  Administrators only if your broker's terminal needs it). Nothing else runs as that user, so a dump policy for it
  affects nothing else, its profile/`%ProgramData%\Stoic` ACL is clean, and RDP sessions of other users cannot
  read its process memory without admin rights.
- **Optional admin step (robust, machine-wide, per executable only)** — in an Administrator PowerShell:
  ```powershell
  foreach ($exe in "terminal64.exe", "powershell.exe") {
      $k = "HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\$exe"
      New-Item -Path $k -Force | Out-Null
      Set-ItemProperty -Path $k -Name DumpCount -Value 0 -Type DWord
  }
  ```
  This disables **only** the local crash dumps of those two executables (per-exe LocalDumps keys override the global
  one); error reporting for everything else is untouched. Remove the two keys to revert.
- What remains: the managed string of a secret until the GC collects it, readable only by a local administrator with
  a debugger attached to the live process.

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
