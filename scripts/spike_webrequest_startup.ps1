# Easy-Connect spike — can MT5's WebRequest allow-list be set without the Options dialog?
#
# N110-6 — this script restarts a terminal, turns Algo Trading on terminal-wide and overwrites config files.
# It therefore runs ONLY against a THROWAWAY PORTABLE COPY of a terminal (never your demo/live terminal):
#
#   1. copy a broker terminal folder (the one with terminal64.exe) to e.g. C:\stoic-spike\MT5
#   2. start it once with  terminal64.exe /portable , log in to a DEMO account, let STOIC-Installer.ps1 run
#      against it (-TerminalPath C:\stoic-spike\MT5 -NoRestart) so STOIC-Server.txt / stoic.set / stoic-start.ini exist
#   3. .\spike_webrequest_startup.ps1 -PortableCopy C:\stoic-spike\MT5 -ServerUrl https://www.stoicaibot.com `
#          [-GoldenConfig "<data folder of a terminal where the URL IS allowed>\config"]
#
# Protocol (verdict = Experts log lines written AFTER each start, never older lines):
#   0. BASELINE  plain stoic-start.ini → MUST show "WebRequest error 4014". If it does not, the URL is already
#                allowed in this copy and every later PASS would be false: the spike ABORTS.
#   A. startup ini with candidate [Experts]/[WebRequest] keys (undocumented; expected to be ignored)
#   B. copy config\common.ini + terminal.ini from a "golden" terminal where the URL was allowed by hand
# The copy's config\ folder is backed up before A/B and RESTORED automatically at the end (also on error).
param(
    [Parameter(Mandatory = $true)][string]$PortableCopy,
    [Parameter(Mandatory = $true)][string]$ServerUrl,
    [string]$GoldenConfig = "",
    [int]$WaitSeconds = 75
)
$ErrorActionPreference = "Stop"
$DataFolder = $PortableCopy.TrimEnd('\')
$exe = Join-Path $DataFolder "terminal64.exe"
if (-not (Test-Path $exe)) { throw "terminal64.exe not found in $DataFolder — pass the PORTABLE COPY folder (terminal64.exe + MQL5 side by side)" }
if ($DataFolder -like "$env:APPDATA*" -or $DataFolder -like "$env:ProgramFiles*" -or $DataFolder -like "${env:ProgramFiles(x86)}*") {
    throw "refusing: $DataFolder is an installed terminal, not a throwaway portable copy (N110-6)"
}
if (-not (Test-Path (Join-Path $DataFolder "stoic-start.ini"))) { throw "stoic-start.ini missing — run STOIC-Installer.ps1 -TerminalPath $DataFolder -NoRestart first" }
if ($ServerUrl -notmatch '^https://') { throw "-ServerUrl must be https" }
$logDir = Join-Path $DataFolder "MQL5\Logs"
$cfg = Join-Path $DataFolder "config"
$backup = "$cfg.spike-backup-$(Get-Date -Format yyyyMMddHHmmss)"

function Stop-Terminal {
    Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { try { (Split-Path $_.Path -Parent).TrimEnd('\') -ieq $DataFolder } catch { $false } } | ForEach-Object {
        $null = $_.CloseMainWindow(); if (-not $_.WaitForExit(60000)) { throw "the spike terminal did not close within 60 s — close it by hand and re-run" }
    }
    Start-Sleep -Seconds 2
}
function Start-Terminal([string]$Ini) {
    Start-Process -FilePath $exe -ArgumentList @("/portable", "/config:`"$Ini`"") -WorkingDirectory $DataFolder | Out-Null
}
function Get-LinesSince([datetime]$Since) {
    # N111-5 — MQL5 Experts-log lines are "<2-char code>\t<n>\tHH:mm:ss.fff\t<source>\t<message>": anchor on the
    # FIRST HH:mm:ss.fff token wherever it sits; keep only lines stamped at/after $Since (older 4014s never count)
    $out = @()
    foreach ($day in @($Since.Date, (Get-Date).Date) | Sort-Object -Unique) {
        $log = Join-Path $logDir ($day.ToString("yyyyMMdd") + ".log")
        if (-not (Test-Path $log)) { continue }
        foreach ($line in ((Get-Content $log -Encoding Unicode -Raw) -split "`n")) {
            $m = [regex]::Match($line, '(\d{2}):(\d{2}):(\d{2})\.(\d{3})')
            if (-not $m.Success) { continue }
            $ts = $day.AddHours([int]$m.Groups[1].Value).AddMinutes([int]$m.Groups[2].Value).AddSeconds([int]$m.Groups[3].Value).AddMilliseconds([int]$m.Groups[4].Value)
            if ($ts -ge $Since) { $out += $line }
        }
    }
    return $out
}
function Verdict([string]$Label, [datetime]$Since) {
    Start-Sleep -Seconds $WaitSeconds
    $lines   = @(Get-LinesSince $Since)
    $started = @($lines | Where-Object { $_ -match 'STOIC Bridge EA v' }).Count -gt 0
    $err4014 = @($lines | Where-Object { $_ -match 'WebRequest error 4014' }).Count -gt 0     # exact EA text (N111-5)
    $hb      = @($lines | Where-Object { $_ -match 'heartbeat|Heartbeat' }).Count
    $ok = $started -and -not $err4014
    Write-Host ("[{0}] {1}  started={2} err4014={3} heartbeatLines={4} (lines since start: {5})" -f $(if ($ok) { "PASS" } else { "FAIL" }), $Label, $started, $err4014, $hb, $lines.Count) -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
    return [pscustomobject]@{ ok = $ok; started = $started; err4014 = $err4014 }
}

$results = [ordered]@{}
# N111-5 — back up config\ BEFORE the first (baseline) start: the baseline run itself rewrites terminal.ini
Copy-Item $cfg $backup -Recurse -Force
try {
    # ── 0. BASELINE — the plain startup config MUST fail with 4014, otherwise the spike proves nothing ──
    Stop-Terminal; $t0 = Get-Date; Start-Terminal (Join-Path $DataFolder "stoic-start.ini")
    $base = Verdict "0 baseline (plain stoic-start.ini)" $t0
    if (-not $base.started) { throw "baseline: the EA did not start at all — fix the install (chart symbol / compile) before probing WebRequest" }
    if (-not $base.err4014) { throw "baseline: NO 4014 error — the URL is already allowed in this copy (or the EA reached the server); a PASS below would be meaningless. Use a fresh copy." }
    $results["0 baseline"] = "FAIL (expected)"

    # ── A. candidate [Experts]/[WebRequest] keys in the startup ini ────────────────────────────────────
    $iniA = Join-Path $DataFolder "stoic-spike-a.ini"
    @"
[Experts]
AllowLiveTrading=1
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
Symbol=$((Select-String -Path (Join-Path $DataFolder 'stoic-start.ini') -Pattern '^Symbol=(.+)$').Matches[0].Groups[1].Value)
Period=M15
"@ | Set-Content -Path $iniA -Encoding ASCII
    Stop-Terminal; $t0 = Get-Date; Start-Terminal $iniA
    $results["A startup-ini keys"] = $(if ((Verdict "A startup-ini [Experts]/[WebRequest] keys" $t0).ok) { "PASS" } else { "FAIL" })

    # ── B. golden config copy (terminal stopped) ───────────────────────────────────────────────────────
    if ($GoldenConfig -and (Test-Path $GoldenConfig)) {
        Stop-Terminal
        foreach ($f in @("common.ini", "terminal.ini")) {
            $src = Join-Path $GoldenConfig $f
            if (Test-Path $src) { Copy-Item $src (Join-Path $cfg $f) -Force }
        }
        $t0 = Get-Date; Start-Terminal (Join-Path $DataFolder "stoic-start.ini")
        $results["B golden config copy"] = $(if ((Verdict "B copy golden config\common.ini+terminal.ini" $t0).ok) { "PASS" } else { "FAIL" })
    } else {
        Write-Host "[SKIP] B golden config copy — pass -GoldenConfig <data folder>\config of a terminal where the URL is allowed" -ForegroundColor DarkGray
    }
} finally {
    # ── restore: stop the copy, put config\ back exactly as it was ──────────────────────────────────────
    try { Stop-Terminal } catch { Write-Host "restore: $($_.Exception.Message)" -ForegroundColor Yellow }
    if (Test-Path $backup) {
        Remove-Item $cfg -Recurse -Force -ErrorAction SilentlyContinue
        Copy-Item $backup $cfg -Recurse -Force
        Remove-Item $backup -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "restored: $cfg from the pre-spike backup" -ForegroundColor DarkGray
    }
    Remove-Item (Join-Path $DataFolder "stoic-spike-a.ini") -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Spike summary (throwaway copy $DataFolder):" -ForegroundColor Cyan
$results.GetEnumerator() | ForEach-Object { Write-Host ("  {0,-28} {1}" -f $_.Key, $_.Value) }
Write-Host "The copy is stopped and its config restored. Nothing on your real terminals was touched." -ForegroundColor DarkGray
