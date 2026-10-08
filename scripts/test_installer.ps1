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

    # v1.5 — several terminals: the RUNNING one (terminal64.exe folder == origin.txt) is offered first;
    # N110-4 — the operator must CONFIRM it (path + login shown); declining falls back to the numbered choice
    function Get-Process { param($Name, $ErrorAction) [pscustomobject]@{ Path = "C:\Program Files\Broker MT5\terminal64.exe" } }
    $t = Resolve-StoicTerminal -Root $tmp -PortableRoots @() -Confirm { "y" } -Prompt { throw "prompt must not be shown when the running terminal is confirmed" }
    Assert-Equal $t.Name $idB "running terminal (origin.txt match) auto-selected among several"
    $t = Resolve-StoicTerminal -Root $tmp -PortableRoots @() -Confirm { "" } -Prompt { "1" }
    Assert-Equal $t.Name $idA "running terminal NOT confirmed (Enter = No) → numbered choice is used"
    function Get-Process { param($Name, $ErrorAction) @() }
    Assert-Null (Get-StoicRunningTerminal -Candidates (@(Get-StoicTerminals -Root $tmp -PortableRoots @()))) "no MT5 running → no auto-pick (prompt path)"
    Remove-Item Function:\Get-Process

    # N110-4 / N111-3 — login detection from the terminal's own journal (UTF-16), REAL MT5 wording ("authorized on")
    New-Item -ItemType Directory -Force (Join-Path $tmp "$idB\logs") | Out-Null
    Set-Content -Path (Join-Path $tmp "$idB\logs\20261007.log") -Encoding Unicode -Value @(
        "CS`t0`t10:00:00.101`tTerminal`tMetaTrader 5 x64 build 4620 started for Broker Ltd",
        "NS`t0`t10:00:01.000`tNetwork`t'11111111': authorized on Broker-Demo through Access Point EU 1 (ping: 46.19 ms)",
        "NS`t0`t10:00:01.200`tNetwork`t'11111111': previous successful authorization performed from 1.2.3.4 on 2026.10.06 09:00:00",
        "NS`t0`t10:05:01.000`tNetwork`t'22222222': authorized on Broker-Demo through Access Point EU 1 (ping: 46.19 ms)")
    Assert-Equal (Get-StoicTerminalLogin (Join-Path $tmp $idB)) "22222222" "last 'authorized on' line wins (MT5 wording)"
    Set-Content -Path (Join-Path $tmp "$idB\logs\20261006.log") -Encoding Unicode -Value @("0`t09:00:00.000`tNetwork`t'33333333': login on Broker-Demo")
    Assert-Equal (Get-StoicTerminalLogin (Join-Path $tmp $idB)) "22222222" "newest log is read first; MT4 'login on' wording still accepted"
    Assert-Equal (Get-StoicTerminalLogin (Join-Path $tmp $idA)) "" "no logs folder → unknown login"

    # N110-3 / N111-6 — chart symbol follows the broker suffix of an OPEN chart in the ACTIVE profile only
    New-Item -ItemType Directory -Force (Join-Path $tmp "$idB\profiles\charts\Default") | Out-Null
    New-Item -ItemType Directory -Force (Join-Path $tmp "$idB\profiles\charts\Scalping") | Out-Null
    New-Item -ItemType Directory -Force (Join-Path $tmp "$idB\config") | Out-Null
    Set-Content -Path (Join-Path $tmp "$idB\profiles\charts\Default\chart01.chr") -Encoding Unicode -Value @("<chart>", "symbol=XAUUSD.m", "period_type=1", "</chart>")
    Set-Content -Path (Join-Path $tmp "$idB\profiles\charts\Default\chart02.chr") -Encoding Unicode -Value @("<chart>", "symbol=EURUSD.m", "period_type=1", "</chart>")
    Set-Content -Path (Join-Path $tmp "$idB\profiles\charts\Scalping\chart01.chr") -Encoding Unicode -Value @("<chart>", "symbol=EURUSD.r", "period_type=1", "</chart>")
    Assert-Equal (Get-StoicChartSymbol -DataFolder (Join-Path $tmp $idB)) "EURUSD.m" "no terminal.ini → 'Default' profile; EURUSD.m preferred over XAUUSD.m"
    Set-Content -Path (Join-Path $tmp "$idB\config\terminal.ini") -Encoding Unicode -Value @("[Charts]", "ProfileLast=Scalping", "MaxBars=50000")
    Assert-Equal (Get-StoicChartSymbol -DataFolder (Join-Path $tmp $idB)) "EURUSD.r" "ProfileLast=Scalping → only the active profile is read"
    Assert-Equal (Get-StoicChartSymbol -DataFolder (Join-Path $tmp $idB) -Preferred "GBPUSD") "EURUSD.r" "no match → first open chart symbol of the active profile"
    Assert-Equal (Get-StoicChartSymbol -DataFolder (Join-Path $tmp $idA)) "EURUSD" "no charts → preferred symbol as-is"
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

Write-Host "Compile log verdict (N105-5)"
$tmpLog = Join-Path ([IO.Path]::GetTempPath()) ("stoic-log-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $tmpLog | Out-Null
try {
    $ok = Join-Path $tmpLog "ok.log"; [IO.File]::WriteAllText($ok, "MetaEditor 5.00 build 4500`r`nResult: 0 errors, 2 warnings`r`n", [Text.Encoding]::Unicode)
    $bad = Join-Path $tmpLog "bad.log"; [IO.File]::WriteAllText($bad, "Result: 3 errors, 0 warnings`r`n", [Text.Encoding]::Unicode)
    $alt = Join-Path $tmpLog "alt.log"; [IO.File]::WriteAllText($alt, "0 error(s), 1 warning(s)`r`n", [Text.Encoding]::UTF8)
    $junk = Join-Path $tmpLog "junk.log"; [IO.File]::WriteAllText($junk, "nothing useful`r`n", [Text.Encoding]::Unicode)
    Assert-Equal (Test-StoicCompileLog -LogPath $ok) $true "0 errors (UTF-16 log) → compiled"
    Assert-Equal (Test-StoicCompileLog -LogPath $bad) $false "3 errors → not compiled"
    Assert-Equal (Test-StoicCompileLog -LogPath $alt) $true "alternate '0 error(s)' format → compiled"
    Assert-Equal (Test-StoicCompileLog -LogPath $junk) $false "unparseable log → not compiled"
    Assert-Equal (Test-StoicCompileLog -LogPath (Join-Path $tmpLog "missing.log")) $false "missing log → not compiled"
} finally {
    Remove-Item -Recurse -Force $tmpLog -ErrorAction SilentlyContinue
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
$bytes = [IO.File]::ReadAllBytes($installer)
if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { Write-Host "  ok   installer saved with a UTF-8 BOM (Windows PowerShell 5.1 reads the unicode glyphs correctly)" -ForegroundColor Green }
else { $script:failures++; Write-Host "  FAIL installer lacks the UTF-8 BOM (N105-5)" -ForegroundColor Red }
if ($body -match 'Remove-Item -Force \$ex5' -and $body -match 'Test-StoicCompileLog -LogPath \$log') { Write-Host "  ok   stale .ex5 removed before compile; compile trusted only on a 0-error log" -ForegroundColor Green }
else { $script:failures++; Write-Host "  FAIL compile path does not remove the stale .ex5 / check the log" -ForegroundColor Red }
# main110 review (N110-2/3/7)
if ($body -notmatch 'ServerUrl=\$' -and $body -match 'STOIC-Server.txt' -and $body -match '\$eaServerUrl') { Write-Host "  ok   preset carries no ServerUrl; STOIC-Server.txt written from the claim response" -ForegroundColor Green }
else { $script:failures++; Write-Host "  FAIL preset still overrides ServerUrl or the drop file is not written from server_url (N110-2)" -ForegroundColor Red }
if ($body -match "cnotmatch '\^https://'" ) { Write-Host "  ok   non-https -ServerUrl is refused (case-sensitive, like the EA)" -ForegroundColor Green }
else { $script:failures++; Write-Host "  FAIL installer accepts a non-https -ServerUrl or checks case-insensitively (N110-2/N111-6)" -ForegroundColor Red }
if ($body -match 'terminal_login\s*=' -and $body -match 'account_mismatch' -and $body.IndexOf('terminal_login') -lt $body.IndexOf('claimResp.bridge_token')) { Write-Host "  ok   terminal login travels in the claim; 409 account_mismatch handled before anything is spent" -ForegroundColor Green }
else { $script:failures++; Write-Host "  FAIL claim does not carry terminal_login / handle account_mismatch (N111-2)" -ForegroundColor Red }
if ($body -notmatch 'AllowDllImport' -and $body -match '\[y/N\]' -and $body -match 'UNMANAGED') { Write-Host "  ok   restart is opt-in with a warning; no terminal-wide AllowDllImport" -ForegroundColor Green }
else { $script:failures++; Write-Host "  FAIL restart prompt/ini not hardened (N110-3)" -ForegroundColor Red }
if ($src -notmatch 'Stop-Process') { Write-Host "  ok   MT5 is never force-killed" -ForegroundColor Green }
else { $script:failures++; Write-Host "  FAIL installer still force-kills MT5 (N110-3)" -ForegroundColor Red }
if ($body -match 'X-STOIC-EA-Version') { Write-Host "  ok   CI .ex5 version header compared with ea_latest_version" -ForegroundColor Green }
else { $script:failures++; Write-Host "  FAIL EX5 download not tied to a version (N110-7)" -ForegroundColor Red }

if ($script:failures -gt 0) { throw "installer tests: $($script:failures) failure(s)" }
Write-Host "installer tests passed" -ForegroundColor Green
