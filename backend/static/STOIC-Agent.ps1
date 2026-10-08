# STOIC VPS Agent v1.0 — per-account PORTABLE MetaTrader 5 terminals, installed from the dashboard,
# kept alive by a watchdog (Easy-Connect Phase 2).
# ============================================================================================
# ONE-TIME PREPARATION (per VPS, by you, ~5 minutes):
#   1. Install the broker's MT5 normally, then copy its program folder to  C:\STOIC\golden\MT5
#   2. Start  C:\STOIC\golden\MT5\terminal64.exe /portable  once, log in to ANY demo account,
#      Tools → Options → Expert Advisors → tick "Allow WebRequest for listed URL" → add your STOIC URL → OK → close MT5.
#      (That allow-list lives in the golden folder and is CLONED into every account terminal.)
#   3. Enrol the agent (Administrator PowerShell):
#        irm https://<your-stoic-host>/api/setup/agent.ps1 | iex
#        Install-StoicAgent -ServerUrl "https://<your-stoic-host>" -EnrollmentCode "<code from Dashboard → VPS → Connect existing VPS>"
#   4. For every MT5 account you will install, type its password ONCE on this VPS (DPAPI-encrypted, never sent anywhere):
#        Set-StoicTerminalLogin -Login 12345678 -Server "Broker-Demo"
# THEN: Dashboard → Accounts → account → Quick Install → "Install on my VPS". Nothing else to do on the VPS.
#
# WHAT THE AGENT DOES (scheduled task "StoicVpsAgent", runs at your logon, every 30 s):
#   * polls signed commands: install_terminal {login, server, pairing_token, server_url, chart_symbol}
#       → clone golden → remove the golden account data → run the public installer against the clone (EA, bridge token,
#         STOIC-Server.txt, stoic-start.ini) → first start with [Login] from the DPAPI store (ini deleted afterwards)
#   * watchdog: terminal process dead → start it; EA heartbeat stale (> 180 s, asked from the server) → graceful restart;
#     at most 3 restarts per hour per terminal, then status "restart_loop" (the dashboard raises an ops alert)
#   * reports every terminal's state to /api/vps/agent/terminals/report (Install Progress shows it)
# Nothing here ever force-kills MT5 (CloseMainWindow + 60 s wait), and no password leaves this machine.
# ============================================================================================
param(
    [switch]$Run,
    [string]$ConfigPath = "$env:ProgramData\Stoic\vps-agent.json"
)

$script:AgentVersion = "1.0"
$script:TaskName = "StoicVpsAgent"
$script:Root = "C:\STOIC"
$script:GoldenDir = "C:\STOIC\golden\MT5"
$script:TerminalsDir = "C:\STOIC\MT5"
$script:LoginsDir = "$env:ProgramData\Stoic\logins"
$script:LogFile = "$env:ProgramData\Stoic\vps-agent.log"
$script:StaleS = 180
$script:MaxRestartsPerHour = 3
$script:Restarts = @{}          # login → [datetime[]] restart stamps (last hour)

try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

function Write-StoicLog([string]$Message, [string]$Level = "INFO") {
    $line = "{0} [{1}] {2}" -f (Get-Date).ToUniversalTime().ToString("o"), $Level, $Message
    try { New-Item -ItemType Directory -Force -Path (Split-Path $script:LogFile) | Out-Null; Add-Content -Path $script:LogFile -Value $line -Encoding UTF8 } catch { }
    if (-not $Run) { Write-Host $line }
}

# ── config (agent token DPAPI-protected, current-user scope == the scheduled task's user) ──────────────────
function Protect-StoicSecret([string]$Plain) { ConvertTo-SecureString $Plain -AsPlainText -Force | ConvertFrom-SecureString }
function Unprotect-StoicSecret([string]$Blob) {
    $ss = ConvertTo-SecureString $Blob
    [Runtime.InteropServices.Marshal]::PtrToStringUni([Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($ss))
}
function Get-StoicAgentConfig {
    if (-not (Test-Path $ConfigPath)) { throw "agent not installed — run Install-StoicAgent first" }
    Get-Content $ConfigPath -Raw | ConvertFrom-Json
}
function Save-StoicAgentConfig($cfg) {
    New-Item -ItemType Directory -Force -Path (Split-Path $ConfigPath) | Out-Null
    $cfg | ConvertTo-Json -Depth 5 | Set-Content -Path $ConfigPath -Encoding UTF8
}
function Invoke-StoicApi($cfg, [string]$Method, [string]$Path, $Body = $null) {
    $headers = @{}
    if ($cfg.cert_fingerprint) { $headers["X-Client-Cert-Fingerprint"] = $cfg.cert_fingerprint }
    $uri = "$($cfg.server_url)$Path"
    if ($null -ne $Body) {
        if ($Body -is [hashtable] -and $cfg.agent_token_enc) { $Body["agent_token"] = Unprotect-StoicSecret $cfg.agent_token_enc }
        return Invoke-RestMethod -Method $Method -Uri $uri -ContentType "application/json" -Headers $headers -Body ($Body | ConvertTo-Json -Depth 6) -TimeoutSec 30
    }
    Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -TimeoutSec 30
}

# ── enrolment ──────────────────────────────────────────────────────────────────────────────────────────────
function Install-StoicAgent {
    param(
        [Parameter(Mandatory = $true)][string]$ServerUrl,
        [string]$EnrollmentCode = "",
        [string]$BootstrapToken = "",
        [string]$GoldenPath = "C:\STOIC\golden\MT5"
    )
    $ServerUrl = $ServerUrl.TrimEnd('/')
    if ($ServerUrl -cnotmatch '^https://') { throw "-ServerUrl must start with https://" }
    if (-not $EnrollmentCode -and -not $BootstrapToken) { throw "pass -EnrollmentCode (Dashboard → VPS → Connect existing VPS) or -BootstrapToken" }
    if (-not (Test-Path (Join-Path $GoldenPath "terminal64.exe"))) {
        Write-Host "  ! golden portable MT5 not found at $GoldenPath — prepare it first (see the header of this script). Enrolling anyway; installs will wait." -ForegroundColor Yellow
    }
    $facts = @{
        bootstrap_token = $BootstrapToken; enrollment_code = $EnrollmentCode
        agent_version = "vps-agent/$script:AgentVersion"; hostname = $env:COMPUTERNAME
        windows_version = (Get-CimInstance Win32_OperatingSystem).Caption
        machine_fingerprint = (Get-CimInstance Win32_ComputerSystemProduct).UUID
        timezone = (Get-TimeZone).Id
    }
    $reg = Invoke-RestMethod -Method POST -Uri "$ServerUrl/api/infra/agent/register" -ContentType "application/json" -Body ($facts | ConvertTo-Json) -TimeoutSec 30
    $cfg = [ordered]@{
        server_url = $ServerUrl; agent_id = $reg.agent_id
        agent_token_enc = Protect-StoicSecret $reg.agent_token
        command_key_enc = $(if ($reg.command_key) { Protect-StoicSecret $reg.command_key } else { $null })
        golden_path = $GoldenPath; terminals_dir = $script:TerminalsDir
        installed_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    Save-StoicAgentConfig $cfg
    New-Item -ItemType Directory -Force -Path $script:TerminalsDir, $script:LoginsDir | Out-Null

    # scheduled task at THIS user's logon (interactive session: terminals stay visible over RDP; auto-logon recommended)
    $self = $PSCommandPath
    if (-not $self) {
        $self = Join-Path $env:ProgramData "Stoic\STOIC-Agent.ps1"
        Invoke-WebRequest -Uri "$ServerUrl/api/setup/agent.ps1" -OutFile $self -UseBasicParsing
    }
    $action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$self`" -Run"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    Unregister-ScheduledTask -TaskName $script:TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $script:TaskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -User $env:USERNAME | Out-Null
    Start-ScheduledTask -TaskName $script:TaskName
    Write-Host "  ✓ STOIC VPS Agent $($reg.agent_id) enrolled; task '$script:TaskName' running (log: $script:LogFile)" -ForegroundColor Green
    Write-Host "  Next: Set-StoicTerminalLogin -Login <mt5 login> -Server <broker server>  for each account, then 'Install on my VPS' in the dashboard." -ForegroundColor White
}

# ── MT5 passwords: typed once here, DPAPI, never transmitted ───────────────────────────────────────────────
function Set-StoicTerminalLogin {
    param([Parameter(Mandatory = $true)][string]$Login, [Parameter(Mandatory = $true)][string]$Server)
    if ($Login -notmatch '^\d{4,12}$') { throw "-Login must be the numeric MT5 account number" }
    $pw = Read-Host "  MT5 password for #$Login on $Server (stored DPAPI-encrypted on THIS machine only)" -AsSecureString
    New-Item -ItemType Directory -Force -Path $script:LoginsDir | Out-Null
    @{ login = $Login; server = $Server; password_enc = ($pw | ConvertFrom-SecureString); saved_at = (Get-Date).ToUniversalTime().ToString("o") } |
        ConvertTo-Json | Set-Content -Path (Join-Path $script:LoginsDir "$Login.json") -Encoding UTF8
    Write-Host "  ✓ login #$Login stored for the agent's first start of that terminal" -ForegroundColor Green
}
function Get-StoicStoredLogin([string]$Login) {
    $p = Join-Path $script:LoginsDir "$Login.json"
    if (-not (Test-Path $p)) { return $null }
    $d = Get-Content $p -Raw | ConvertFrom-Json
    [pscustomobject]@{ login = $d.login; server = $d.server; password = (Unprotect-StoicSecret $d.password_enc) }
}

# ── terminals ──────────────────────────────────────────────────────────────────────────────────────────────
function Get-StoicTerminalDir([string]$Login) { Join-Path $script:TerminalsDir "account-$Login" }
function Get-StoicTerminalProcess([string]$Dir) {
    $d = $Dir.TrimEnd('\')
    @(Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { try { (Split-Path $_.Path -Parent).TrimEnd('\') -ieq $d } catch { $false } })
}
function Stop-StoicTerminal([string]$Dir) {
    foreach ($p in (Get-StoicTerminalProcess $Dir)) {
        $null = $p.CloseMainWindow()
        if (-not $p.WaitForExit(60000)) { Write-StoicLog "terminal in $Dir did not close in 60 s — NOT force-killing" "WARN"; return $false }
    }
    return $true
}
function Start-StoicTerminal([string]$Dir, [string]$Ini) {
    $exe = Join-Path $Dir "terminal64.exe"
    Start-Process -FilePath $exe -ArgumentList @("/portable", "/config:`"$Ini`"") -WorkingDirectory $Dir -WindowStyle Minimized | Out-Null
}
function Send-StoicTerminalReport($cfg, [hashtable]$Report) {
    try { Invoke-StoicApi $cfg "POST" "/api/vps/agent/terminals/report" $Report | Out-Null } catch { Write-StoicLog "report failed: $($_.Exception.Message)" "WARN" }
}

function Invoke-StoicInstallTerminal($cfg, $params) {
    $login = "$($params.login)"; $dir = Get-StoicTerminalDir $login
    $base = @{ account_id = "$($params.account_id)"; login = $login; directory = $dir }
    if (-not (Test-Path (Join-Path $cfg.golden_path "terminal64.exe"))) {
        Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "golden portable MT5 missing at $($cfg.golden_path) — prepare it on the VPS (see agent script header)" })
        return $false
    }
    Send-StoicTerminalReport $cfg ($base + @{ status = "installing"; detail = "cloning the golden terminal" })
    if (-not (Stop-StoicTerminal $dir)) { Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "an existing terminal in $dir would not close" }); return $false }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    # clone: everything but logs/, the golden account's saved logins and its open charts (fresh profile → our startup ini decides)
    & robocopy $cfg.golden_path $dir /E /NFL /NDL /NJH /NJS /XD "$($cfg.golden_path)\logs" "$($cfg.golden_path)\MQL5\Logs" /XF "accounts.dat" | Out-Null
    if ($LASTEXITCODE -ge 8) { Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "robocopy exit $LASTEXITCODE while cloning" }); return $false }
    Remove-Item (Join-Path $dir "config\accounts.dat") -Force -ErrorAction SilentlyContinue

    # the public installer does the EA / token / server file / startup ini — exactly as a manual install would
    try {
        $src = Invoke-WebRequest -Uri "$($cfg.server_url)/api/setup/installer.ps1" -UseBasicParsing
        $expected = "$($src.Headers['X-STOIC-SHA256'])".ToLower()
        $sha = (Get-FileHash -InputStream $src.RawContentStream -Algorithm SHA256).Hash.ToLower()
        if ($expected -and $sha -ne $expected) { throw "installer hash mismatch ($sha ≠ $expected)" }
        Invoke-Expression $src.Content
        Install-Stoic -Token "$($params.pairing_token)" -ServerUrl "$($params.server_url)" -TerminalPath $dir -NoRestart -ChartSymbol "$($params.chart_symbol)" -TerminalLogin $login
    } catch {
        Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "installer: $($_.Exception.Message)" }); return $false
    }
    if (-not (Test-Path (Join-Path $dir "MQL5\Files\STOIC-Token.txt"))) {
        Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "installer did not write the bridge token (see its output in $script:LogFile)" }); return $false
    }

    # first start: [Login] from the DPAPI store if the operator typed it; the ini with the password is removed afterwards
    $ini = Join-Path $dir "stoic-start.ini"
    $stored = Get-StoicStoredLogin $login
    $firstIni = $ini
    if ($stored) {
        $firstIni = Join-Path $dir "stoic-first-start.ini"
        $server = $(if ($params.server) { "$($params.server)" } else { $stored.server })
        (Get-Content $ini -Raw) + "`r`n[Login]`r`nLogin=$login`r`nPassword=$($stored.password)`r`nServer=$server`r`n" | Set-Content -Path $firstIni -Encoding ASCII
    }
    Start-StoicTerminal $dir $firstIni
    if ($stored) { Start-Sleep -Seconds 90; Remove-Item $firstIni -Force -ErrorAction SilentlyContinue }
    $status = $(if ($stored) { "running" } else { "awaiting_login" })
    $detail = $(if ($stored) { "started with the stored login; waiting for the first EA heartbeat" } else { "started — no stored password: log #$login in once on the VPS (or run Set-StoicTerminalLogin and re-install)" })
    Send-StoicTerminalReport $cfg ($base + @{ status = $status; detail = $detail; pid = (Get-StoicTerminalProcess $dir | Select-Object -First 1).Id })
    return $true
}

function Invoke-StoicWatchdog($cfg) {
    $st = Invoke-StoicApi $cfg "POST" "/api/vps/agent/terminals/status" @{}
    foreach ($t in @($st.terminals)) {
        $login = "$($t.login)"; $dir = $(if ($t.directory) { "$($t.directory)" } else { Get-StoicTerminalDir $login })
        if (-not (Test-Path (Join-Path $dir "terminal64.exe"))) { continue }
        if ($t.status -in @("failed", "stopped", "restart_loop")) { continue }
        $procs = Get-StoicTerminalProcess $dir
        $ini = Join-Path $dir "stoic-start.ini"
        $base = @{ account_id = "$($t.account_id)"; login = $login; directory = $dir }
        $hour = (Get-Date).AddHours(-1)
        $script:Restarts[$login] = @($script:Restarts[$login] | Where-Object { $_ -gt $hour })
        if ($procs.Count -eq 0) {
            if ($script:Restarts[$login].Count -ge $script:MaxRestartsPerHour) { Send-StoicTerminalReport $cfg ($base + @{ status = "restart_loop"; detail = "process keeps dying"; restarts_last_hour = $script:Restarts[$login].Count }); continue }
            Write-StoicLog "terminal #$login not running — starting"
            Start-StoicTerminal $dir $ini; $script:Restarts[$login] += Get-Date
            Send-StoicTerminalReport $cfg ($base + @{ status = "restarted"; detail = "process was not running — started"; restarts_last_hour = $script:Restarts[$login].Count })
        } elseif ($t.restart_wanted) {
            if ($script:Restarts[$login].Count -ge $script:MaxRestartsPerHour) {
                Send-StoicTerminalReport $cfg ($base + @{ status = "restart_loop"; detail = "EA heartbeat still stale after $($script:Restarts[$login].Count) restarts this hour"; restarts_last_hour = $script:Restarts[$login].Count }); continue
            }
            Write-StoicLog "terminal #$login heartbeat stale ($($t.heartbeat_age_s) s) — graceful restart"
            if (Stop-StoicTerminal $dir) {
                Start-Sleep -Seconds 3; Start-StoicTerminal $dir $ini; $script:Restarts[$login] += Get-Date
                Send-StoicTerminalReport $cfg ($base + @{ status = "restarted"; detail = "EA heartbeat stale — terminal restarted"; restarts_last_hour = $script:Restarts[$login].Count })
            }
        }
    }
    return @($st.terminals).Count
}

function Invoke-StoicCommands($cfg) {
    $resp = Invoke-StoicApi $cfg "POST" "/api/infra/agent/commands/poll" @{}
    foreach ($c in @($resp.commands)) {
        # signed command sequence (iter-122 P3): verify HMAC(agent_id|command_id|seq|command) with the enrolment command_key
        if ($cfg.command_key_enc -and $c.sig) {
            $key = [Text.Encoding]::UTF8.GetBytes((Unprotect-StoicSecret $cfg.command_key_enc))
            $h = New-Object System.Security.Cryptography.HMACSHA256 (,$key)
            $calc = ([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes("$($cfg.agent_id)|$($c.command_id)|$($c.seq)|$($c.command)"))) -replace '-', '').ToLower()
            if ($calc -ne "$($c.sig)".ToLower()) { Write-StoicLog "command $($c.command_id) signature mismatch — ignored" "WARN"; continue }
        }
        $ok = $false; $detail = ""
        try {
            switch ($c.command) {
                "install_terminal" { $ok = Invoke-StoicInstallTerminal $cfg $c.params; $detail = $(if ($ok) { "installed" } else { "see terminal report" }) }
                "restart_terminal" {
                    $dir = Get-StoicTerminalDir "$($c.params.login)"
                    $ok = (Stop-StoicTerminal $dir); if ($ok) { Start-StoicTerminal $dir (Join-Path $dir "stoic-start.ini") }; $detail = "restarted"
                }
                "run_diagnostics" { $ok = $true; $detail = "terminals=" + ((Get-ChildItem $script:TerminalsDir -Directory -ErrorAction SilentlyContinue | Measure-Object).Count) }
                default { $ok = $false; $detail = "unsupported by vps-agent" }
            }
        } catch { $ok = $false; $detail = $_.Exception.Message }
        try { Invoke-StoicApi $cfg "POST" "/api/infra/agent/commands/ack" @{ command_id = $c.command_id; ok = $ok; detail = "$detail" } | Out-Null } catch { Write-StoicLog "ack failed: $($_.Exception.Message)" "WARN" }
    }
}

function Send-StoicHeartbeat($cfg, [int]$Managed) {
    $running = @(Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { try { $_.Path -like "$($script:TerminalsDir)\*" } catch { $false } }).Count
    $os = Get-CimInstance Win32_OperatingSystem
    $disk = Get-PSDrive -Name C
    $metrics = @{
        agent_version = "vps-agent/$script:AgentVersion"; hostname = $env:COMPUTERNAME
        golden_ready = (Test-Path (Join-Path $cfg.golden_path "terminal64.exe"))
        terminals_managed = $Managed; terminals_running = $running; mt5_processes = $running
        disk_free_gb = [math]::Round($disk.Free / 1GB, 1)
        ram_percent = [math]::Round((1 - ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize)) * 100, 0)
    }
    try { Invoke-StoicApi $cfg "POST" "/api/infra/agent/heartbeat" @{ metrics = $metrics } | Out-Null } catch { Write-StoicLog "heartbeat failed: $($_.Exception.Message)" "WARN" }
}

function Start-StoicAgentLoop {
    $cfg = Get-StoicAgentConfig
    Write-StoicLog "STOIC VPS Agent $script:AgentVersion started ($($cfg.agent_id))"
    while ($true) {
        $managed = 0
        try { Invoke-StoicCommands $cfg } catch { Write-StoicLog "commands: $($_.Exception.Message)" "WARN" }
        try { $managed = Invoke-StoicWatchdog $cfg } catch { Write-StoicLog "watchdog: $($_.Exception.Message)" "WARN" }
        Send-StoicHeartbeat $cfg $managed
        Start-Sleep -Seconds 30
    }
}

if ($Run) { Start-StoicAgentLoop }
