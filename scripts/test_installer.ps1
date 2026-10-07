# N104-1 — behavioural tests for backend/static/STOIC-Installer.ps1 (run with pwsh on the Windows CI runner).
# The static Python checks cannot execute PowerShell; this exercises the URL-building and terminal-selection
# code paths that broke in main103 ("$eaScriptUrl?v=" → empty variable → download of "=1.60").
$ErrorActionPreference = "Stop"
$installer = Join-Path $PSScriptRoot "..\backend\static\STOIC-Installer.ps1"
. $installer

$script:failures = 0
function Assert-Equal($actual, $expected, [string]$what) {
    if ("$actual" -ne "$expected") { $script:failures++; Write-Host "  FAIL $what : got '$actual' expected '$expected'" -ForegroundColor Red }
    else { Write-Host "  ok   $what" -ForegroundColor Green }
}
function Assert-Null($actual, [string]$what) {
    if ($null -ne $actual) { $script:failures++; Write-Host "  FAIL $what : expected nothing, got '$actual'" -ForegroundColor Red }
    else { Write-Host "  ok   $what" -ForegroundColor Green }
}

Write-Host "URL building"
Assert-Equal (Get-StoicEaDownloadUrl -BaseUrl "https://stoic.example/api/ea-script" -Version "1.60") "https://stoic.example/api/ea-script?v=1.60" "download url carries ?v=<version>"
Assert-Equal (Get-StoicEaDownloadUrl -BaseUrl "https://stoic.example/api/ea-script?x=1" -Version "1.60") "https://stoic.example/api/ea-script?x=1&v=1.60" "existing query string gets &v="
Assert-Equal (Get-StoicEaDownloadUrl -BaseUrl "https://stoic.example/api/ea-script" -Version "") "https://stoic.example/api/ea-script" "no version → plain url"
Assert-Equal (Get-StoicWebRequestHost -HeartbeatUrl "https://stoic.example/api/bridge/heartbeat") "https://stoic.example" "WebRequest host = scheme://authority"
Assert-Equal (Get-StoicWebRequestHost -HeartbeatUrl "https://stoic.example:8443/api/x") "https://stoic.example:8443" "WebRequest host keeps the port"

Write-Host "Terminal selection (temp data folders)"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("stoic-installer-test-" + [guid]::NewGuid().ToString("N"))
$idA = "0123456789ABCDEF0123456789ABCDEF"
$idB = "FEDCBA9876543210FEDCBA9876543210"
foreach ($d in @("$idA\MQL5\Experts", "$idB\MQL5\Experts", "Community", "NOTAHEXID\MQL5\Experts")) {
    New-Item -ItemType Directory -Force (Join-Path $tmp $d) | Out-Null
}
Set-Content -Path (Join-Path $tmp "$idA\origin.txt") -Value "" -Encoding UTF8                      # empty origin.txt must not crash
Set-Content -Path (Join-Path $tmp "$idB\origin.txt") -Value "C:\Program Files\Broker MT5" -Encoding UTF8
try {
    $found = @(Get-StoicTerminals -Root $tmp -PortableRoots @())
    Assert-Equal $found.Count 2 "two 32-hex data folders discovered (Community / non-hex skipped)"
    Assert-Equal (Get-StoicTerminalOrigin (Join-Path $tmp $idA)) "" "empty origin.txt → empty label"
    Assert-Equal (Get-StoicTerminalOrigin (Join-Path $tmp $idB)) "C:\Program Files\Broker MT5" "origin.txt first line"
    Assert-Equal (Get-StoicTerminalOrigin (Join-Path $tmp "Community")) "" "missing origin.txt → empty label"

    $t = Resolve-StoicTerminal -TerminalId $idA -Root $tmp -PortableRoots @()
    Assert-Equal $t.Name $idA "-TerminalId picks that terminal"
    $t = Resolve-StoicTerminal -TerminalPath (Join-Path $tmp $idB) -Root $tmp -PortableRoots @()
    Assert-Equal $t.Name $idB "-TerminalPath picks that terminal"
    $t = Resolve-StoicTerminal -Root $tmp -PortableRoots @() -Prompt { "2" }
    Assert-Equal $t.Name $idB "interactive choice 2 → second terminal"
    Assert-Null (Resolve-StoicTerminal -Root $tmp -PortableRoots @() -Prompt { "x" }) "invalid choice → nothing selected"
    Assert-Null (Resolve-StoicTerminal -Root $tmp -PortableRoots @() -Prompt { "9" }) "out-of-range choice → nothing selected"
    Assert-Null (Resolve-StoicTerminal -TerminalPath (Join-Path $tmp "Community") -Root $tmp -PortableRoots @()) "path without MQL5\Experts → refused"
    Assert-Null (Resolve-StoicTerminal -TerminalId "00000000000000000000000000000000" -Root $tmp -PortableRoots @()) "unknown id → refused"
    Assert-Null (Resolve-StoicTerminal -Root (Join-Path $tmp "nothing-here") -PortableRoots @()) "no terminals → refused"

    # portable-mode terminal (MQL5 next to terminal64.exe) under a portable root
    $portable = Join-Path $tmp "portable\Broker MT5"
    New-Item -ItemType Directory -Force (Join-Path $portable "MQL5\Experts") | Out-Null
    Set-Content -Path (Join-Path $portable "terminal64.exe") -Value "" -Encoding ASCII
    $t = Resolve-StoicTerminal -Root (Join-Path $tmp "nothing-here") -PortableRoots @((Join-Path $tmp "portable"))
    Assert-Equal $t.FullName $portable "single portable terminal auto-selected"
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

Write-Host "Install-Stoic ordering (source)"
$src = Get-Content $installer -Raw
$body = $src.Substring($src.IndexOf("function Install-Stoic"))
if ($body.IndexOf("Resolve-StoicTerminal -TerminalPath") -lt $body.IndexOf("/api/setup/claim-pairing")) { Write-Host "  ok   terminal is chosen BEFORE the pairing token is claimed" -ForegroundColor Green }
else { $script:failures++; Write-Host "  FAIL terminal must be chosen before the pairing token is claimed" -ForegroundColor Red }
if ($body -notmatch '\$iniPath') { Write-Host "  ok   installer no longer writes terminal.ini" -ForegroundColor Green }
else { $script:failures++; Write-Host "  FAIL installer still writes terminal.ini (ineffective WebRequest setting)" -ForegroundColor Red }
if ($body -notmatch '"\$eaScriptUrl\?v=') { Write-Host "  ok   no raw `"`$eaScriptUrl?v=`" interpolation" -ForegroundColor Green }
else { $script:failures++; Write-Host "  FAIL raw `$eaScriptUrl?v= interpolation present" -ForegroundColor Red }

if ($script:failures -gt 0) { throw "installer tests: $($script:failures) failure(s)" }
Write-Host "installer tests passed" -ForegroundColor Green
