# M117-2 - behavioural test of the VPS agent's restart-ledger durability + recovery transition
# (degraded -> recovering -> recovered). Runs under Windows PowerShell 5.1 on the Windows CI runner
# (shell: powershell) AND under pwsh 7 locally: no 7-only syntax below.
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\test_agent_recovery.ps1
$ErrorActionPreference = "Stop"
$agent = Join-Path $PSScriptRoot "..\backend\static\STOIC-Agent.ps1"
. $agent     # dot-source: defines the functions; nothing runs without -Run

$script:failures = 0
function Assert-True($cond, [string]$what) {
    if (-not $cond) { $script:failures++; Write-Host "  FAIL $what" -ForegroundColor Red } else { Write-Host "  ok   $what" -ForegroundColor Green }
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("stoic-agent-recovery-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
$script:LogFile = Join-Path $tmp "agent.log"
$script:RestartLedger = Join-Path $tmp "restarts.json"
$script:Restarts = @{}
$script:Degraded = $null
$script:RecoverySince = $null
$script:RecoveryWindowMinutes = 10

try {
    Write-Host "1. healthy save (empty ledger, one-item array, several entries) - byte-exact read-back"
    Assert-True (Save-StoicRestartLedger) "empty ledger saves"
    Assert-True (Test-StoicLedgerReadBack $script:RestartLedger (@{} | ConvertTo-Json -Depth 3)) "empty object read-back is byte-exact"
    $script:Restarts["12345678"] = @((Get-Date).AddMinutes(-5))                   # ONE-item array: the 5.1/7 formatting quirk case
    Assert-True (Save-StoicRestartLedger) "one-item array saves"
    $script:Restarts["87654321"] = @((Get-Date).AddMinutes(-50), (Get-Date).AddMinutes(-1), (Get-Date).AddHours(-3))   # the 3 h stamp is pruned
    Assert-True (Save-StoicRestartLedger) "multi-entry ledger saves"
    $disk = Get-Content -Raw -Path $script:RestartLedger | ConvertFrom-Json
    Assert-True (@($disk."87654321").Count -eq 2) "stamps older than 1 h are pruned on save"
    Assert-True ($null -eq $script:Degraded) "not degraded after healthy saves"

    Write-Host "2. ledger not persistable -> DEGRADED, no restarts"
    $script:RestartLedger = Join-Path (Join-Path $tmp "no-such-dir") "restarts.json"   # parent missing => write fails
    Assert-True (-not (Save-StoicRestartLedger)) "save fails when the ledger cannot be written"
    Assert-True ($script:Degraded -like "restart ledger unsaved:*") "degraded reason recorded ($script:Degraded)"
    Assert-True ($null -eq $script:RecoverySince) "no recovery window while the write fails"

    Write-Host "3. durable write again -> RECOVERING (stability window), still degraded"
    $script:RestartLedger = Join-Path $tmp "restarts.json"
    Assert-True (Save-StoicRestartLedger) "durable write succeeds"
    Assert-True ($null -ne $script:RecoverySince) "stability window started"
    Assert-True ($script:Degraded -like "recovering:*") "status stays degraded with detail 'recovering' ($script:Degraded)"
    Assert-True (Save-StoicRestartLedger) "second durable write inside the window"
    Assert-True ($script:Degraded -like "recovering:*") "still recovering before the window elapses"

    Write-Host "4. a failure inside the window resets it"
    $script:RestartLedger = Join-Path (Join-Path $tmp "no-such-dir") "restarts.json"
    $null = Save-StoicRestartLedger
    Assert-True ($script:Degraded -like "restart ledger unsaved:*" -and $null -eq $script:RecoverySince) "window cleared, degraded again"

    Write-Host "5. window elapsed -> RECOVERED"
    $script:RestartLedger = Join-Path $tmp "restarts.json"
    $null = Save-StoicRestartLedger                       # starts a fresh window
    $script:RecoverySince = (Get-Date).AddMinutes(-11)    # simulate 11 min of durable saves
    Assert-True (Save-StoicRestartLedger) "save after the window"
    Assert-True ($null -eq $script:Degraded -and $null -eq $script:RecoverySince) "agent no longer degraded; window cleared"

    Write-Host "6. read-back detects a tampered/partial file"
    [IO.File]::WriteAllText($script:RestartLedger, '{"x":', (New-Object Text.UTF8Encoding $false))
    Assert-True (-not (Test-StoicLedgerReadBack $script:RestartLedger (@{} | ConvertTo-Json -Depth 3))) "mismatch reported for different bytes"

    Write-Host "7. crash-dump policy function is narrow (static)"
    $src = Get-Content -Raw -Path $agent
    Assert-True ($src -notmatch 'Set-ItemProperty -Path \$wer -Name Disabled') "no user-wide WER Disabled=1"
    Assert-True ($src -match 'Remove-ItemProperty -Path \$wer -Name Disabled') "1.3 Disabled=1 is reverted on upgrade"
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) assertion(s) failed" -ForegroundColor Red; exit 1 }
Write-Host "agent recovery tests passed (PowerShell $($PSVersionTable.PSVersion))" -ForegroundColor Green
