# M122-P1 — drop-file encoding gate. MUST run under Windows PowerShell 5.1 (powershell.exe, not pwsh): 5.1 is
# what operators run on their MT5 hosts and the ONLY shell where `Set-Content -Encoding UTF8` writes a BOM.
# 1. writes STOIC-Token/Server/Installation.txt through the installer's own Write-StoicDropFile
# 2. asserts no file starts with EF BB BF
# 3. parses the files with the EA's exact reader logic (scripts/ea_dropfile_reader.py) and compares the values
# 4. negative control: the pre-1.8 writer (Set-Content -Encoding UTF8) under 5.1 + the pre-1.63 reader (--legacy)
#    yields the '#' comment line as the token — proves this gate detects the bug class.
$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ne 5) { throw "run this under Windows PowerShell 5.1 (got $($PSVersionTable.PSVersion))" }
$installer = Join-Path $PSScriptRoot "..\backend\static\STOIC-Installer.ps1"
$reader    = Join-Path $PSScriptRoot "ea_dropfile_reader.py"
. $installer

$token = "k3XbQ2m9Vp0sT7wLzA4cR8yE1nH6uJ5gD2fK9qW0xZs"       # nosec — synthetic test fixture, 43 chars = token_urlsafe(32) shape, never issued
$inst  = "inst_0123456789ab"
$url   = "https://stoicaibot.com"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("stoic-dropfiles-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $tmp | Out-Null

Write-Host "installer writer under Windows PowerShell $($PSVersionTable.PSVersion)"
Write-StoicDropFile -Path (Join-Path $tmp "STOIC-Token.txt") -Value $token -Comments @("STOIC bridge token — keep this file confidential.", "Account: OnEquity #1083440 (demo)", "Generated: test")
Write-StoicDropFile -Path (Join-Path $tmp "STOIC-Server.txt") -Value $url -Comments @("STOIC server URL — the EA reads this when its ServerUrl input is left at default.", "Generated: test")
Write-StoicDropFile -Path (Join-Path $tmp "STOIC-Installation.txt") -Value $inst -Comments @("STOIC installation id — proves WHICH EA install is talking.", "Generated: test")
Set-Content -Path (Join-Path $tmp "STOIC-Proof.txt") -Value ("a" * 64) -Encoding ASCII     # unchanged writer (ASCII = no BOM)

foreach ($f in @("STOIC-Token.txt", "STOIC-Server.txt", "STOIC-Installation.txt", "STOIC-Proof.txt")) {
    $p = Join-Path $tmp $f
    if (-not (Test-StoicDropFileNoBom -Path $p)) { throw "$f starts with a UTF-8 BOM" }
    $bytes = [IO.File]::ReadAllBytes($p)
    if ($bytes[0] -ne 0x23 -and $bytes[0] -ne 0x61) { throw "$f first byte is 0x$($bytes[0].ToString('X2')), expected '#' or the value" }
    Write-Host "  ok   $f — no BOM, first byte 0x$($bytes[0].ToString('X2'))"
}

Write-Host "EA reader (exact port) on the 5.1-written files"
python $reader --files-dir $tmp --expect-no-bom --expect-token $token --expect-installation $inst --expect-server $url --expect-proof ("a" * 64)
if ($LASTEXITCODE -ne 0) { throw "EA reader did not recover the installer's values (exit $LASTEXITCODE)" }

Write-Host "negative control: pre-1.8 writer (Set-Content -Encoding UTF8) + pre-1.63 reader (--legacy)"
$old = Join-Path $tmp "legacy"
New-Item -ItemType Directory -Force $old | Out-Null
@"
# STOIC bridge token — keep this file confidential.
# Account: OnEquity #1083440 (demo)
$token
"@ | Set-Content -Path (Join-Path $old "STOIC-Token.txt") -Encoding UTF8
$b = [IO.File]::ReadAllBytes((Join-Path $old "STOIC-Token.txt"))
if (-not ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)) { throw "expected 5.1 Set-Content -Encoding UTF8 to write a BOM (the bug this gate guards) — got 0x$($b[0].ToString('X2'))" }
$legacy = python $reader --files-dir $old --legacy --json | ConvertFrom-Json
if ($legacy.results.token.value -ne $token -and $legacy.results.token.value.Length -gt 0) {
    Write-Host "  ok   legacy reader on the BOM file took '$($legacy.results.token.value.Substring(0, 20))…' (length $($legacy.results.token.value.Length)) — the exact production failure" -ForegroundColor Green
} else { throw "negative control failed: legacy reader on a BOM file should NOT recover the token" }
$fixed = python $reader --files-dir $old --json | ConvertFrom-Json
if ($fixed.results.token.value -ne $token) { throw "1.63 reader must recover the token even from a BOM file (got '$($fixed.results.token.value)')" }
Write-Host "  ok   1.63 reader recovers the token from the BOM file (operators on an old installer are not bricked)" -ForegroundColor Green

Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
Write-Host "drop-file gate passed" -ForegroundColor Green
