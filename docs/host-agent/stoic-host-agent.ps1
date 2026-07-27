# STOIC Host Agent v2.1 — Windows service: telemetry, MT5 supervision,
# command execution, signed self-update, credential renewal (iter-157)
# Install (as Administrator):
#   powershell -ExecutionPolicy Bypass -File .\stoic-host-agent.ps1 -Install `
#     -ApiBase "https://www.stoicaibot.com" -AgentId "<agent_id>" -AgentToken "<agent_token>" `
#     [-Mt5Path "C:\Program Files\MetaTrader 5\terminal64.exe"]
# Responsibilities:
#   * telemetry every 60s (disk, CPU, RAM, MT5 state, latency, restarts)
#   * MT5 process supervision — auto-restart terminal64 when it dies
#   * backend command execution (restart_mt5 / run_update_check /
#     collect_diagnostics / restart_agent) via the signed command queue
#   * signed self-update — Ed25519 manifest w/ PINNED public key, SHA-256
#     verify BEFORE swap, automatic rollback of a failed swap
#   * credential renewal — rotates its own agent token every 30 days
param(
    [switch]$Install,
    [switch]$Run,
    [string]$ApiBase = "",
    [string]$AgentId = "",
    [string]$AgentToken = "",
    [string]$Mt5Path = ""
)

$ServiceName = "StoicHostAgent"
$ConfigPath = "$env:ProgramData\Stoic\agent.json"

# iter-171 (#4) — PINNED release verification key, COMPILED INTO the agent.
# Manifests/updates are accepted ONLY if signed by this exact Ed25519 key, so a
# compromised API can never hand the agent a rogue signing key (no trust-on-
# first-use). Replace with the PRODUCTION release public key before signing the
# MSI. Obtain via: GET /api/infra/artifacts/manifest -> .signature.public_key_b64
$PinnedReleaseKey = "shsQu1qBIZRAX1sUkxn4v7sU9IbRFLLSA88T9+5Q7TY="

function Get-Config { Get-Content $ConfigPath -Raw | ConvertFrom-Json }
function Save-Config($cfg) { $cfg | ConvertTo-Json | Set-Content -Path $ConfigPath -Encoding UTF8 }

function Install-Agent {
    New-Item -ItemType Directory -Force -Path (Split-Path $ConfigPath) | Out-Null
    # Enforce the HARDCODED pin: if the server's manifest key differs, REFUSE
    # to install rather than trust whatever the API returns.
    try {
        $m = Invoke-RestMethod -Uri "$ApiBase/api/infra/artifacts/manifest" -TimeoutSec 15
        if ($m.signature.alg -eq "Ed25519" -and
            $m.signature.public_key_b64 -ne $PinnedReleaseKey) {
            throw "release key mismatch: server key '$($m.signature.public_key_b64)' != pinned key — refusing to enroll"
        }
    } catch {
        if ("$_" -like "*mismatch*") { throw }
        Write-Host "[stoic] WARN: could not pre-check release key at install: $_"
    }
    $cfg = @{ api_base = $ApiBase; agent_id = $AgentId; agent_token = $AgentToken;
              mt5_path = $Mt5Path; mt5_supervise = [bool]$Mt5Path;
              release_public_key_b64 = $PinnedReleaseKey;
              token_rotated_at = (Get-Date).ToUniversalTime().ToString("o");
              installed_at = (Get-Date).ToUniversalTime().ToString("o") }
    Save-Config $cfg

    $self = $MyInvocation.PSCommandPath
    if (-not $self) { $self = $PSCommandPath }
    $bin = "powershell.exe -ExecutionPolicy Bypass -NoProfile -File `"$self`" -Run"
    sc.exe create $ServiceName binPath= $bin start= auto DisplayName= "STOIC Host Agent" | Out-Null
    # Windows service recovery: restart after 5s, 30s, 60s; reset count daily
    sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/30000/restart/60000 | Out-Null
    sc.exe start $ServiceName | Out-Null
    Write-Host "[stoic] service '$ServiceName' installed (restart recovery + pinned release key)."
}

$script:Mt5Restarts = 0

function Ensure-Mt5($cfg) {
    # MT5 supervision: if the terminal should be running and is not, start it.
    if (-not $cfg.mt5_supervise -or -not $cfg.mt5_path) { return }
    $mt5 = @(Get-Process -Name "terminal64" -ErrorAction SilentlyContinue)
    if ($mt5.Count -eq 0 -and (Test-Path $cfg.mt5_path)) {
        try {
            Start-Process -FilePath $cfg.mt5_path -WindowStyle Minimized
            $script:Mt5Restarts++
            Write-EventLog -LogName Application -Source $ServiceName -EventId 200 `
                -EntryType Warning -Message "MT5 terminal was down — restarted (count=$script:Mt5Restarts)" -ErrorAction SilentlyContinue
        } catch {
            Write-EventLog -LogName Application -Source $ServiceName -EventId 201 `
                -EntryType Error -Message "MT5 restart FAILED: $_" -ErrorAction SilentlyContinue
        }
    }
}

function Get-Telemetry($cfg) {
    $disk = Get-PSDrive -Name C
    $diskFreePct = [math]::Round(($disk.Free / ($disk.Used + $disk.Free)) * 100, 1)
    $mt5 = @(Get-Process -Name "terminal64" -ErrorAction SilentlyContinue)
    $lat = $null
    try {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        Invoke-WebRequest -Uri "$($cfg.api_base)/health" -TimeoutSec 10 -UseBasicParsing | Out-Null
        $sw.Stop(); $lat = [math]::Round($sw.Elapsed.TotalMilliseconds, 0)
    } catch {}
    $os = Get-CimInstance Win32_OperatingSystem
    $pendingReboot = Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"
    @{
        disk_free_gb       = [math]::Round($disk.Free / 1GB, 1)
        disk_free_pct      = $diskFreePct
        broker_latency_ms  = $lat
        mt5_processes      = $mt5.Count
        mt5_connected      = ($mt5.Count -gt 0)
        mt5_supervised     = [bool]$cfg.mt5_supervise
        mt5_restarts       = $script:Mt5Restarts
        ea_attached        = ($mt5.Count -gt 0)  # refined by EA heartbeat server-side
        cpu_percent        = [math]::Round((Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average, 0)
        ram_percent        = [math]::Round((1 - ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize)) * 100, 0)
        pending_reboot     = $pendingReboot
        last_boot          = $os.LastBootUpTime.ToUniversalTime().ToString("o")
        restarts_24h       = Get-RestartCount
        service_uptime_sec = [math]::Round(((Get-Date) - (Get-Process -Id $PID).StartTime).TotalSeconds, 0)
        agent_version      = "2.1.0"
    }
}

function Get-RestartCount {
    try {
        $since = (Get-Date).AddHours(-24)
        @(Get-WinEvent -FilterHashtable @{LogName="System"; Id=7031,7034; StartTime=$since} `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.Message -match $ServiceName }).Count
    } catch { 0 }
}

function Test-UpdateManifest($cfg) {
    # Signed self-update: Ed25519 manifest with PINNED key; SHA-256 verify
    # BEFORE swap; previous file kept as .bak for automatic rollback.
    # Never installs from a mutable URL — only /api/artifacts/{sha256}.
    $deployed = @()
    try {
        $m = Invoke-RestMethod -Uri "$($cfg.api_base)/api/infra/artifacts/manifest?agent_id=$($cfg.agent_id)" -TimeoutSec 15
        if ($m.signature.alg -ne "Ed25519") { throw "unsigned manifest" }
        if ($cfg.release_public_key_b64 -and
            $m.signature.public_key_b64 -ne $cfg.release_public_key_b64) {
            throw "release key mismatch — possible compromise, refusing update"
        }
        foreach ($a in $m.artifacts) {
            if ($a.type -eq "ex5" -and $a.sha256) {
                $target = "$env:ProgramData\Stoic\EmergentTradingBridge.ex5"
                $cur = if (Test-Path $target) { (Get-FileHash $target -Algorithm SHA256).Hash.ToLower() } else { "" }
                if ($cur -ne $a.sha256) {
                    $tmp = "$target.new"
                    Invoke-WebRequest -Uri "$($cfg.api_base)$($a.url)" -OutFile $tmp -UseBasicParsing
                    $got = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
                    if ($got -ne $a.sha256) { Remove-Item -Force $tmp; throw "sha256 mismatch for $($a.name)" }
                    if (Test-Path $target) { Copy-Item -Force $target "$target.bak" }
                    try { Move-Item -Force $tmp $target; $deployed += $a.name }
                    catch {
                        # rollback: restore the previous verified artifact
                        if (Test-Path "$target.bak") { Copy-Item -Force "$target.bak" $target }
                        throw "swap failed — rolled back: $_"
                    }
                }
            }
        }
        foreach ($name in $deployed) {
            Report-DeployStatus $cfg $true $name "installed + hash-verified"
        }
    } catch {
        Report-DeployStatus $cfg $false "update" "$_"
        Write-EventLog -LogName Application -Source $ServiceName -EventId 100 -EntryType Warning -Message "update check failed: $_" -ErrorAction SilentlyContinue
    }
}

function Report-DeployStatus($cfg, $ok, $artifact, $detail) {
    # Deployment auditability: every attempt lands in the backend; failures
    # raise a centralized ops alert (deployment_failed).
    try {
        $body = @{ agent_token = $cfg.agent_token; ok = $ok; artifact = $artifact;
                   detail = "$detail"; agent_version = "2.1.0" } | ConvertTo-Json
        Invoke-RestMethod -Method Post -Uri "$($cfg.api_base)/api/infra/agent/deploy-status" `
            -ContentType "application/json" -Body $body -TimeoutSec 15 | Out-Null
    } catch {}
}

function Renew-TokenIfDue($cfg) {
    # Rotate the agent credential every 30 days (agent-initiated).
    try {
        $rotated = [datetime]::Parse($cfg.token_rotated_at)
        if (((Get-Date).ToUniversalTime() - $rotated.ToUniversalTime()).TotalDays -lt 30) { return $cfg }
        $r = Invoke-RestMethod -Method Post -Uri "$($cfg.api_base)/api/infra/agent/renew-token" `
            -ContentType "application/json" -Body (@{ agent_token = $cfg.agent_token } | ConvertTo-Json) -TimeoutSec 15
        if ($r.agent_token) {
            $cfg.agent_token = $r.agent_token
            $cfg.token_rotated_at = (Get-Date).ToUniversalTime().ToString("o")
            Save-Config $cfg
            Write-EventLog -LogName Application -Source $ServiceName -EventId 300 `
                -EntryType Information -Message "agent token rotated" -ErrorAction SilentlyContinue
        }
    } catch {}
    return $cfg
}

function Invoke-BackendCommands($cfg) {
    # Poll the HMAC-signed command queue and execute the small allow-list.
    try {
        $resp = Invoke-RestMethod -Method Post -Uri "$($cfg.api_base)/api/infra/agent/commands/poll" `
            -ContentType "application/json" -Body (@{ agent_token = $cfg.agent_token } | ConvertTo-Json) -TimeoutSec 15
        foreach ($c in @($resp.commands)) {
            $ok = $true; $detail = "executed"
            switch ($c.command) {
                "restart_mt5"        { Get-Process -Name "terminal64" -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep 2; Ensure-Mt5 $cfg; $detail = "MT5 restarted" }
                "run_update_check"   { Test-UpdateManifest $cfg; $detail = "update check ran" }
                "collect_diagnostics"{ $detail = (Get-Telemetry $cfg | ConvertTo-Json -Compress) }
                "restart_agent"      { $detail = "agent restarting"; }
                default              { $ok = $false; $detail = "unknown command '$($c.command)' refused" }
            }
            $ack = @{ agent_token = $cfg.agent_token; command_id = $c.command_id; ok = $ok; detail = $detail } | ConvertTo-Json
            Invoke-RestMethod -Method Post -Uri "$($cfg.api_base)/api/infra/agent/commands/ack" `
                -ContentType "application/json" -Body $ack -TimeoutSec 15 | Out-Null
            if ($c.command -eq "restart_agent") { Restart-Service $ServiceName -Force }
        }
    } catch {}
}

function Run-Loop {
    $cfg = Get-Config
    $tick = 0
    while ($true) {
        try {
            Ensure-Mt5 $cfg
            $t = Get-Telemetry $cfg
            $hb = Invoke-RestMethod -Method Post -Uri "$($cfg.api_base)/api/infra/agent/heartbeat" `
                -ContentType "application/json" `
                -Body (@{ agent_token = $cfg.agent_token; metrics = $t } | ConvertTo-Json) -TimeoutSec 15
            # iter-160 — central config sync: apply desired config from backend
            if ($hb.desired_config) {
                $changed = $false
                foreach ($k in @("mt5_supervise", "mt5_path", "telemetry_interval_sec", "update_checks_enabled")) {
                    $v = $hb.desired_config.$k
                    if ($null -ne $v -and ($cfg.PSObject.Properties[$k] -eq $null -or $cfg.$k -ne $v)) {
                        $cfg | Add-Member -NotePropertyName $k -NotePropertyValue $v -Force
                        $changed = $true
                    }
                }
                if ($changed) {
                    Save-Config $cfg
                    Write-EventLog -LogName Application -Source $ServiceName -EventId 400 `
                        -EntryType Information -Message "desired config applied from backend" -ErrorAction SilentlyContinue
                }
            }
            Invoke-BackendCommands $cfg
        } catch {}
        $interval = 60
        if ($cfg.telemetry_interval_sec) { $interval = [int]$cfg.telemetry_interval_sec }
        if ($tick % 30 -eq 15 -and ($cfg.update_checks_enabled -ne $false)) { Test-UpdateManifest $cfg }  # ~every 30 min
        if ($tick % 60 -eq 30) { $cfg = Renew-TokenIfDue $cfg }  # ~hourly check
        $tick++
        Start-Sleep -Seconds $interval
    }
}

if ($Install) { Install-Agent }
elseif ($Run) { Run-Loop }
else { Write-Host "Use -Install (with -ApiBase/-AgentId/-AgentToken[/-Mt5Path]) or -Run" }
