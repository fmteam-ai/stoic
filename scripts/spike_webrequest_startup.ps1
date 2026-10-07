# Easy-Connect spike — can MT5's WebRequest allow-list be set without the Options dialog?
# Run ON THE WINDOWS VPS in PowerShell (as the user that runs MT5), with the STOIC EA already installed
# by STOIC-Installer.ps1 (so STOIC-Server.txt / stoic.set / stoic-start.ini exist in the data folder).
#
#   .\spike_webrequest_startup.ps1 -DataFolder "C:\Users\me\AppData\Roaming\MetaQuotes\Terminal\<32hex>" `
#                                  -ServerUrl "https://www.stoicaibot.com" [-GoldenConfig "<data folder of a terminal where the URL IS allowed>\config"]
#
# It tries, one after another, and prints PASS/FAIL per attempt (verdict = the Experts log shows the EA
# started and NO "WebRequest error 4014" within the wait window):
#   A. startup ini with candidate [Experts] WebRequest keys (undocumented; expected to be ignored)
#   B. copying config\common.ini + terminal.ini from a "golden" terminal where the URL was allowed by hand
#      (MetaQuotes keeps the list encrypted/bound to the terminal — may or may not survive the copy)
# Nothing here touches STOIC servers; it only restarts the local terminal with alternative configs.
param(
    [Parameter(Mandatory = $true)][string]$DataFolder,
    [Parameter(Mandatory = $true)][string]$ServerUrl,
    [string]$GoldenConfig = "",
    [int]$WaitSeconds = 75
)
$ErrorActionPreference = "Stop"
$origin = (Get-Content (Join-Path $DataFolder "origin.txt") -ErrorAction SilentlyContinue | Select-Object -First 1)
$exe = if ($origin) { Join-Path $origin.Trim() "terminal64.exe" } else { Join-Path $DataFolder "terminal64.exe" }
if (-not (Test-Path $exe)) { throw "terminal64.exe not found for $DataFolder" }
$exeDir = Split-Path $exe -Parent
$logDir = Join-Path $DataFolder "MQL5\Logs"

function Stop-Terminal {
    Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { (Split-Path $_.Path -Parent) -ieq $exeDir } | ForEach-Object {
        $null = $_.CloseMainWindow(); if (-not $_.WaitForExit(20000)) { Stop-Process -Id $_.Id -Force }
    }
    Start-Sleep -Seconds 2
}
function Start-Terminal([string]$Ini) {
    $args = @("/config:`"$Ini`""); if ($exeDir -ieq $DataFolder.TrimEnd('\')) { $args = @("/portable") + $args }
    Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $exeDir | Out-Null
}
function Verdict([string]$Label, [datetime]$Since) {
    Start-Sleep -Seconds $WaitSeconds
    $log = Join-Path $logDir ((Get-Date).ToString("yyyyMMdd") + ".log")
    $text = if (Test-Path $log) { Get-Content $log -Encoding Unicode -Raw } else { "" }
    $tail = ($text -split "`n") | Where-Object { $_ -match '\d{2}:\d{2}:\d{2}' } | Select-Object -Last 400
    $started = ($tail | Where-Object { $_ -match 'STOIC Bridge EA v' }).Count -gt 0
    $err4014 = ($tail | Where-Object { $_ -match '4014' }).Count -gt 0
    $hb      = ($tail | Where-Object { $_ -match 'heartbeat|Heartbeat' }).Count -gt 0
    $ok = $started -and -not $err4014
    Write-Host ("[{0}] {1}  started={2} err4014={3} heartbeatLines={4}" -f ($(if ($ok) { "PASS" } else { "FAIL" }), $Label, $started, $err4014, $hb)) -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
    return $ok
}

$results = @{}
# ── A. candidate [Experts] keys in the startup ini ────────────────────────────────────────────────
$iniA = Join-Path $DataFolder "stoic-spike-a.ini"
@"
[Experts]
AllowLiveTrading=1
AllowDllImport=0
Enabled=1
Account=0
Profile=0
WebRequest=1
AllowWebRequest=1
WebRequestUrl=$ServerUrl
WebRequestUrl0=$ServerUrl
[WebRequest]
Url0=$ServerUrl
[StartUp]
Expert=EmergentTradingBridge
ExpertParameters=stoic.set
Symbol=EURUSD
Period=M15
"@ | Set-Content -Path $iniA -Encoding ASCII
Stop-Terminal; $t0 = Get-Date; Start-Terminal $iniA
$results["A startup-ini keys"] = Verdict "A startup-ini [Experts]/[WebRequest] keys" $t0

# ── B. golden config copy (terminal stopped) ─────────────────────────────────────────────────────
if ($GoldenConfig -and (Test-Path $GoldenConfig)) {
    Stop-Terminal
    $cfg = Join-Path $DataFolder "config"
    Copy-Item $cfg "$cfg.spike-backup-$(Get-Date -Format yyyyMMddHHmmss)" -Recurse -Force
    foreach ($f in @("common.ini", "terminal.ini")) {
        $src = Join-Path $GoldenConfig $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $cfg $f) -Force }
    }
    $t0 = Get-Date; Start-Terminal (Join-Path $DataFolder "stoic-start.ini")
    $results["B golden config copy"] = Verdict "B copy golden config\common.ini+terminal.ini" $t0
} else {
    Write-Host "[SKIP] B golden config copy — pass -GoldenConfig <data folder>\config of a terminal where the URL is allowed" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Spike summary:" -ForegroundColor Cyan
$results.GetEnumerator() | ForEach-Object { Write-Host ("  {0,-28} {1}" -f $_.Key, $(if ($_.Value) { "PASS" } else { "FAIL" })) }
Write-Host "Restore: the terminal is running with the last config; re-run STOIC-Installer or start MT5 normally. config backups: $DataFolder\config.spike-backup-*" -ForegroundColor DarkGray
