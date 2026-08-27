# STOIC Host Agent bootstrap — laid down by the signed MSI.
# Copies the bridge EA into every detected MT5 terminal's Experts folder
# and registers a daily clock-health task (w32tm resync + report).
param(
    [string]$BackendUrl = "https://www.stoicaibot.com"
)
$ErrorActionPreference = "Stop"

$src = Join-Path $PSScriptRoot "EmergentTradingBridge.mq5"
$terminals = Get-ChildItem "$env:APPDATA\MetaQuotes\Terminal" -Directory -ErrorAction SilentlyContinue
$installed = 0
foreach ($t in $terminals) {
    $experts = Join-Path $t.FullName "MQL5\Experts"
    if (Test-Path $experts) {
        Copy-Item $src -Destination $experts -Force
        $installed++
        Write-Host "Installed EA into $experts"
    }
}
if ($installed -eq 0) {
    Write-Warning "No MT5 terminal data folders found — install MetaTrader 5 first."
}

# NTP/clock health: force a resync now and register a daily resync task so
# the client_time_ms heartbeat telemetry stays honest.
try {
    w32tm /resync /nowait | Out-Null
    schtasks /Create /F /SC DAILY /TN "STOIC Clock Sync" `
        /TR "w32tm /resync /nowait" /ST 06:00 | Out-Null
    Write-Host "Clock-sync task registered (daily 06:00)."
} catch {
    Write-Warning "Could not register clock-sync task: $_"
}
Write-Host "STOIC Host Agent bootstrap complete. Backend: $BackendUrl"
