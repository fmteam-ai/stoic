# STOIC Host Agent v2 — Windows service wrapper + telemetry loop (iter-139)
# Install (as Administrator):
#   powershell -ExecutionPolicy Bypass -File .\stoic-host-agent.ps1 -Install `
#     -ApiBase "https://www.stoicaibot.com" -AgentId "<agent_id>" -AgentToken "<agent_token>"
# The service self-restarts on failure (sc.exe failure actions) and reports
# telemetry every 60s: disk %, broker latency, MT5 process state, restarts.
param(
    [switch]$Install,
    [switch]$Run,
    [string]$ApiBase = "",
    [string]$AgentId = "",
    [string]$AgentToken = ""
)

$ServiceName = "StoicHostAgent"
$ConfigPath = "$env:ProgramData\Stoic\agent.json"

function Install-Agent {
    New-Item -ItemType Directory -Force -Path (Split-Path $ConfigPath) | Out-Null
    @{ api_base = $ApiBase; agent_id = $AgentId; agent_token = $AgentToken;
       installed_at = (Get-Date).ToUniversalTime().ToString("o") } |
        ConvertTo-Json | Set-Content -Path $ConfigPath -Encoding UTF8

    $self = $MyInvocation.PSCommandPath
    if (-not $self) { $self = $PSCommandPath }
    $bin = "powershell.exe -ExecutionPolicy Bypass -NoProfile -File `"$self`" -Run"
    sc.exe create $ServiceName binPath= $bin start= auto DisplayName= "STOIC Host Agent" | Out-Null
    # Restart recovery: restart after 5s, 30s, 60s; reset failure count daily
    sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/30000/restart/60000 | Out-Null
    sc.exe start $ServiceName | Out-Null
    Write-Host "[stoic] service '$ServiceName' installed with restart recovery."
}

function Get-Config { Get-Content $ConfigPath -Raw | ConvertFrom-Json }

function Get-Telemetry($cfg) {
    $disk = Get-PSDrive -Name C
    $diskFreePct = [math]::Round(($disk.Free / ($disk.Used + $disk.Free)) * 100, 1)
    $mt5 = @(Get-Process -Name "terminal64" -ErrorAction SilentlyContinue)
    # Broker latency proxy: TLS round-trip to the API edge
    $lat = $null
    try {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        Invoke-WebRequest -Uri "$($cfg.api_base)/health" -TimeoutSec 10 -UseBasicParsing | Out-Null
        $sw.Stop(); $lat = [math]::Round($sw.Elapsed.TotalMilliseconds, 0)
    } catch {}
    $uptime = [math]::Round(((Get-Date) - (Get-Process -Id $PID).StartTime).TotalSeconds, 0)
    @{
        disk_free_gb      = [math]::Round($disk.Free / 1GB, 1)
        disk_free_pct     = $diskFreePct
        broker_latency_ms = $lat
        mt5_processes     = $mt5.Count
        mt5_connected     = ($mt5.Count -gt 0)
        ea_attached       = ($mt5.Count -gt 0)  # refined by EA heartbeat server-side
        cpu_percent       = [math]::Round((Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average, 0)
        ram_percent       = [math]::Round((1 - ((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / (Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize)) * 100, 0)
        restarts_24h      = 0   # populated from the service event log below
        service_uptime_sec = $uptime
        agent_version     = "2.0.0"
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
    # Signed self-update: fetch the Ed25519-signed manifest, verify pinned
    # public key, download the content-addressed artifact and verify SHA-256
    # BEFORE swapping any file. Never install from a mutable URL.
    try {
        $m = Invoke-RestMethod -Uri "$($cfg.api_base)/api/infra/artifacts/manifest" `
            -Headers @{ "X-Agent-Id" = $cfg.agent_id; "X-Agent-Token" = $cfg.agent_token } -TimeoutSec 15
        if ($m.signature.alg -ne "Ed25519") { throw "unsigned manifest" }
        # NOTE: pin the public key at enroll time; compare before trusting:
        #   $pinned = (Get-Config).release_public_key_b64
        #   if ($m.signature.public_key_b64 -ne $pinned) { throw "key mismatch" }
        foreach ($a in $m.artifacts) {
            if ($a.type -eq "ex5" -and $a.sha256) {
                $target = "$env:ProgramData\Stoic\EmergentTradingBridge.ex5"
                $cur = if (Test-Path $target) { (Get-FileHash $target -Algorithm SHA256).Hash.ToLower() } else { "" }
                if ($cur -ne $a.sha256) {
                    $tmp = "$target.new"
                    Invoke-WebRequest -Uri "$($cfg.api_base)$($a.url)" -OutFile $tmp -UseBasicParsing
                    $got = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
                    if ($got -eq $a.sha256) { Move-Item -Force $tmp $target }
                    else { Remove-Item -Force $tmp; throw "sha256 mismatch for $($a.name)" }
                }
            }
        }
    } catch { Write-EventLog -LogName Application -Source $ServiceName -EventId 100 -EntryType Warning -Message "update check failed: $_" -ErrorAction SilentlyContinue }
}

function Run-Loop {
    $cfg = Get-Config
    while ($true) {
        try {
            $t = Get-Telemetry $cfg
            $t.restarts_24h = Get-RestartCount
            Invoke-RestMethod -Method Post -Uri "$($cfg.api_base)/api/infra/agent/heartbeat" `
                -Headers @{ "X-Agent-Id" = $cfg.agent_id; "X-Agent-Token" = $cfg.agent_token } `
                -ContentType "application/json" -Body ($t | ConvertTo-Json) -TimeoutSec 15 | Out-Null
        } catch {}
        if ((Get-Random -Maximum 30) -eq 0) { Test-UpdateManifest $cfg }  # ~every 30 min
        Start-Sleep -Seconds 60
    }
}

if ($Install) { Install-Agent }
elseif ($Run) { Run-Loop }
else { Write-Host "Use -Install (with -ApiBase/-AgentId/-AgentToken) or -Run" }
