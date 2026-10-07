# STOIC-Installer.ps1 — one-shot MetaTrader 5 EA installer for STOIC
# ============================================================================
# Usage on a Windows MT5 host (your PC or broker VPS):
#
#   irm https://your-stoic-host/api/setup/installer.ps1 | iex
#   Install-Stoic -Token "abc123..." -ServerUrl "https://your-stoic-host"
#
# OR download + run locally (the file only DEFINES Install-Stoic — dot-source it first):
#
#   . .\STOIC-Installer.ps1
#   Install-Stoic -Token "abc123..." -ServerUrl "https://your-stoic-host" -TerminalPath "<data folder>"
#
# What it does:
#   1. Picks ONE MT5 terminal data folder (-TerminalPath / -TerminalId, or a choice when
#      several are found) BEFORE the one-time pairing token is spent (N104-1/N104-4).
#   2. Calls /api/setup/claim-pairing with your token, gets back the
#      bridge_token + heartbeat URL.
#   3. Downloads the latest EmergentTradingBridge.mq5 from the server.
#   4. Copies it into the terminal's MQL5\Experts\ folder, writes the bridge token into
#      MQL5\Files\STOIC-Token.txt (the EA reads it on attach if no token is hard-coded)
#      and installs / compiles the .ex5.
#   5. Prints the ONE manual step: allow the STOIC URL for WebRequest in MT5
#      (Tools → Options → Expert Advisors). Writing it into terminal.ini is ineffective —
#      MT5 keeps that setting elsewhere and a running terminal overwrites the file (N104-4).
#
# After running, the user only needs to:
#   - Add the WebRequest URL (Tools → Options → Expert Advisors) — once per terminal.
#   - Open MT5, drag the EmergentTradingBridge EA onto any chart.
#   - Confirm AutoTrading is ON (green play button).
#
# The bridge will pick up the token from MQL5\Files\STOIC-Token.txt
# automatically (no manual paste).
# ============================================================================

# ── r26 P1-02 · installer device key (RSA-3072 / RSA-PSS-SHA256) ─────────────
# The installer proves possession of the deployed EX5 with a SIGNATURE from a key
# that only this Windows user profile holds (DPAPI-protected). The public key is
# enrolled while redeeming the one-time dashboard pairing token; the bridge token
# (which the EA also holds) can never attest anything.
Add-Type -AssemblyName System.Security

function Get-StoicDeviceKeyPath {
    $dir = Join-Path $env:APPDATA "STOIC"
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    return (Join-Path $dir "device.key.dpapi")
}

function New-StoicDeviceKey {
    $rsa = New-Object System.Security.Cryptography.RSACng(3072)
    $xmlPriv = $rsa.ToXmlString($true)
    $xmlPub  = $rsa.ToXmlString($false)
    $rsa.Dispose()
    $protected = [System.Security.Cryptography.ProtectedData]::Protect(
        [System.Text.Encoding]::UTF8.GetBytes($xmlPriv), $null,
        [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    [System.IO.File]::WriteAllText((Get-StoicDeviceKeyPath), [Convert]::ToBase64String($protected))
    return $xmlPub
}

function Get-StoicDeviceKey {
    $b64 = [System.IO.File]::ReadAllText((Get-StoicDeviceKeyPath))
    $xmlPriv = [System.Text.Encoding]::UTF8.GetString(
        [System.Security.Cryptography.ProtectedData]::Unprotect(
            [Convert]::FromBase64String($b64), $null,
            [System.Security.Cryptography.DataProtectionScope]::CurrentUser))
    $rsa = New-Object System.Security.Cryptography.RSACng
    $rsa.FromXmlString($xmlPriv)
    return $rsa
}

function Get-StoicDevicePublicKey {
    # A15-2 — KEEP the device key across pairings: one VPS = one device identity, however many
    # terminals it hosts. Pairing terminal B must not invalidate terminal A's enrolment.
    # Pass -RotateDeviceKey to mint a fresh key deliberately.
    if ($script:RotateDeviceKey -or -not (Test-Path (Get-StoicDeviceKeyPath))) { return (New-StoicDeviceKey) }
    $rsa = Get-StoicDeviceKey
    try { return $rsa.ToXmlString($false) } finally { $rsa.Dispose() }
}

function ConvertTo-StoicJsonString([string]$v) {
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    foreach ($ch in $v.ToCharArray()) {
        switch ($ch) {
            '"'  { [void]$sb.Append('\"') }
            '\' { [void]$sb.Append('\\') }
            "`n" { [void]$sb.Append('\n') }
            "`r" { [void]$sb.Append('\r') }
            "`t" { [void]$sb.Append('\t') }
            default {
                if ([int]$ch -lt 0x20) { [void]$sb.Append(('\u{0:x4}' -f [int]$ch)) } else { [void]$sb.Append($ch) }
            }
        }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Get-StoicCanonicalProof($p) {
    # MUST equal Python json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)
    # over exactly these six fields (server: device_attestation.canonical)
    $caps = ($p.capabilities | ForEach-Object { ConvertTo-StoicJsonString $_ }) -join ','
    $json = '{"capabilities":[' + $caps + '],' +
            '"ex5_sha256":'        + (ConvertTo-StoicJsonString $p.ex5_sha256) + ',' +
            '"installation_id":'   + (ConvertTo-StoicJsonString $p.installation_id) + ',' +
            '"nonce":'             + (ConvertTo-StoicJsonString $p.nonce) + ',' +
            '"terminal_identity":' + (ConvertTo-StoicJsonString $p.terminal_identity) + ',' +
            '"ts":' + [string][int64]$p.ts + '}'
    return [System.Text.Encoding]::UTF8.GetBytes($json)
}

function Invoke-StoicAttestation {
    param([string]$ServerUrl, [string]$InstallationId, [string]$TerminalIdentity, [string]$Ex5Sha256, [string[]]$Capabilities)
    $chal = Invoke-RestMethod -Uri "$ServerUrl/api/infra/attestation/challenge" -Method Post -ContentType "application/json" `
        -Body (@{ installation_id = $InstallationId } | ConvertTo-Json -Compress)
    $proof = @{
        installation_id   = $InstallationId
        nonce             = [string]$chal.nonce
        terminal_identity = $TerminalIdentity
        ex5_sha256        = $Ex5Sha256.ToLower()
        capabilities      = @($Capabilities)
        ts                = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    }
    $bytes = Get-StoicCanonicalProof $proof
    $rsa = Get-StoicDeviceKey
    try {
        $sig = $rsa.SignData($bytes, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
                             [System.Security.Cryptography.RSASignaturePadding]::Pss)
    } finally { $rsa.Dispose() }
    $proof.signature = [Convert]::ToBase64String($sig)
    return (Invoke-RestMethod -Uri "$ServerUrl/api/infra/attestation/verify" -Method Post -ContentType "application/json" `
        -Body ($proof | ConvertTo-Json -Compress))
}

# ── N104-1 / N104-4 · pure helpers (exercised by scripts/test_installer.ps1 on the Windows runner) ──
function Get-StoicEaDownloadUrl {
    # interpolating `$eaScriptUrl` followed by `?v=` parsed `eaScriptUrl?v` as ONE (empty) variable → URL "=1.60"
    param([Parameter(Mandatory = $true)][string]$BaseUrl, [string]$Version)
    if (-not $Version) { return $BaseUrl }
    $sep = if ($BaseUrl.Contains("?")) { "&" } else { "?" }
    return "${BaseUrl}${sep}v=${Version}"
}

function Get-StoicWebRequestHost {
    param([Parameter(Mandatory = $true)][string]$HeartbeatUrl)
    $u = [Uri]$HeartbeatUrl
    return $u.Scheme + "://" + $u.Authority
}

function Get-StoicTerminals {
    # every MT5 data folder (32-hex id under %APPDATA%\MetaQuotes\Terminal) + portable-mode installs
    param([string]$Root = (Join-Path $env:APPDATA "MetaQuotes\Terminal"),
          [string[]]$PortableRoots = @("C:\Program Files", "C:\Program Files (x86)", "$env:LOCALAPPDATA\Programs"))
    $found = @()
    if ($Root -and (Test-Path $Root)) {
        # Terminal data folders have a 32-char hex GUID name (skip Community/Help/etc).
        $found += @(Get-ChildItem $Root -Directory | Where-Object {
            $_.Name -match '^[0-9A-F]{32}$' -and (Test-Path (Join-Path $_.FullName "MQL5\Experts"))
        })
    }
    # portable-mode terminals keep MQL5 next to terminal64.exe
    foreach ($r in $PortableRoots) {
        if ($r -and (Test-Path $r)) {
            $found += @(Get-ChildItem $r -Directory -ErrorAction SilentlyContinue | Where-Object {
                (Test-Path (Join-Path $_.FullName "terminal64.exe")) -and (Test-Path (Join-Path $_.FullName "MQL5\Experts"))
            })
        }
    }
    return $found     # callers wrap in @(): 0 → empty, 1 → one DirectoryInfo (a unary-comma return would nest the array)
}

function Resolve-StoicTerminal {
    # ONE terminal: explicit path / id, the only one found, or the operator's choice. $null = nothing chosen.
    param([string]$TerminalPath, [string]$TerminalId,
          [string]$Root = (Join-Path $env:APPDATA "MetaQuotes\Terminal"),
          [string[]]$PortableRoots = @("C:\Program Files", "C:\Program Files (x86)", "$env:LOCALAPPDATA\Programs"),
          [scriptblock]$Prompt = { Read-Host "    terminal number (or re-run with -TerminalPath / -TerminalId)" })
    if ($TerminalPath) {
        # explicit data folder (also covers portable-mode terminals: <install dir>\MQL5)
        if (-not (Test-Path (Join-Path $TerminalPath "MQL5\Experts"))) {
            Write-Host "    ✗ -TerminalPath $TerminalPath has no MQL5\Experts folder (point at the terminal DATA folder: File → Open Data Folder)" -ForegroundColor Red
            return $null
        }
        return (Get-Item $TerminalPath)
    }
    if ($TerminalId) {
        $cand = Join-Path $Root $TerminalId
        if (-not (Test-Path (Join-Path $cand "MQL5\Experts"))) {
            Write-Host "    ✗ -TerminalId $TerminalId not found under $Root" -ForegroundColor Red
            return $null
        }
        return (Get-Item $cand)
    }
    $found = @(Get-StoicTerminals -Root $Root -PortableRoots $PortableRoots)
    if ($found.Count -eq 0) {
        Write-Host "    ✗ No MetaTrader 5 terminal found (looked under $Root and portable install folders)." -ForegroundColor Red
        Write-Host "    Install MT5 from your broker first, then re-run (or pass -TerminalPath)." -ForegroundColor DarkGray
        return $null
    }
    if ($found.Count -eq 1) { return $found[0] }
    # A15-2 — never write one account's token into EVERY terminal: make the operator choose
    Write-Host "    Several terminals found — a pairing token belongs to ONE account. Choose the terminal for it:" -ForegroundColor Yellow
    for ($i = 0; $i -lt $found.Count; $i++) {
        $label = Get-StoicTerminalOrigin $found[$i].FullName
        Write-Host ("    [{0}] {1}  {2}" -f ($i + 1), $found[$i].FullName, $label) -ForegroundColor White
    }
    $pick = & $Prompt
    if (-not ("$pick" -match '^[0-9]+$') -or [int]$pick -lt 1 -or [int]$pick -gt $found.Count) {
        Write-Host "    ✗ no valid choice — nothing written, pairing token NOT used" -ForegroundColor Red
        return $null
    }
    return $found[[int]$pick - 1]
}

function Test-StoicCompileLog {
    # N105-5 — $true ONLY when a MetaEditor log exists and reports 0 errors ("Result: 0 errors, N warnings" /
    # "0 error(s), N warning(s)"). MetaEditor writes UTF-16; fall back to the default decoding.
    param([Parameter(Mandatory = $true)][string]$LogPath)
    if (-not (Test-Path $LogPath)) { return $false }
    $text = ""
    try { $text = Get-Content $LogPath -Encoding Unicode -Raw -ErrorAction Stop } catch { $text = "" }
    if (-not $text -or $text -notmatch 'error') { try { $text = Get-Content $LogPath -Raw -ErrorAction Stop } catch { $text = "" } }
    if (-not $text) { return $false }
    $m = [regex]::Match($text, '(?i)result:?\s*(\d+)\s*errors?')
    if (-not $m.Success) { $m = [regex]::Match($text, '(?i)(\d+)\s*error\(s\)') }
    if (-not $m.Success) { return $false }
    return ([int]$m.Groups[1].Value -eq 0)
}

function Get-StoicTerminalOrigin {
    # first non-empty line of <data folder>\origin.txt (install folder of THIS terminal) — "" when absent/empty (N104-4)
    param([Parameter(Mandatory = $true)][string]$DataFolder)
    $origin = Join-Path $DataFolder "origin.txt"
    if (-not (Test-Path $origin)) { return "" }
    $line = @(Get-Content $origin -ErrorAction SilentlyContinue | Where-Object { "$_".Trim() } | Select-Object -First 1)
    if ($line.Count -eq 0) { return "" }
    return "$($line[0])".Trim()
}

function Install-Stoic {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Token,

        [Parameter(Mandatory = $true)]
        [string]$ServerUrl,

        [string]$Hostname = $env:COMPUTERNAME,

        # A15-2 — install into ONE terminal: full data-folder path, or the 32-hex terminal id
        # under %APPDATA%\MetaQuotes\Terminal. With several terminals and neither given, you choose.
        [string]$TerminalPath,
        [string]$TerminalId,

        [switch]$RotateDeviceKey,
        [switch]$NoCompile
    )

    $InstallerVersion = "1.4"
    $ServerUrl = $ServerUrl.TrimEnd('/')
    $script:RotateDeviceKey = [bool]$RotateDeviceKey
    $devicePublicKey = Get-StoicDevicePublicKey

    Write-Host ""
    Write-Host "===========================================" -ForegroundColor Yellow
    Write-Host " STOIC · MT5 BRIDGE AUTO-INSTALLER  v$InstallerVersion" -ForegroundColor Yellow
    Write-Host "===========================================" -ForegroundColor Yellow
    Write-Host ""

    # ── 1. Choose the MT5 terminal — BEFORE the one-time token is spent (N104-1/N104-4) ──
    Write-Host "[1/5] Locating the MetaTrader 5 terminal ..." -ForegroundColor Cyan
    $terminal = Resolve-StoicTerminal -TerminalPath $TerminalPath -TerminalId $TerminalId
    if (-not $terminal) { return }
    $terminals = @($terminal)
    Write-Host "    • $($terminal.FullName)" -ForegroundColor White
    Write-Host ""

    # ── 2. Claim pairing token ────────────────────────────────────────
    Write-Host "[2/5] Claiming pairing token at $ServerUrl ..." -ForegroundColor Cyan
    try {
        $claimResp = Invoke-RestMethod `
            -Method POST `
            -Uri "$ServerUrl/api/setup/claim-pairing" `
            -ContentType "application/json" `
            -Body (@{
                token             = $Token
                hostname          = $Hostname
                installer_version = $InstallerVersion
                device_key        = @{ algorithm = "RSA-PSS-SHA256"; public_key = $devicePublicKey }
            } | ConvertTo-Json -Depth 4)
    } catch {
        $msg = ""
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            try { $msg = ($_.ErrorDetails.Message | ConvertFrom-Json).detail.message } catch {}
        }
        if (-not $msg) { $msg = $_.Exception.Message }
        Write-Host "    ✗ FAILED: $msg" -ForegroundColor Red
        Write-Host ""
        Write-Host "    The pairing token may be expired or already used." -ForegroundColor DarkGray
        Write-Host "    Generate a fresh one from the STOIC dashboard:" -ForegroundColor DarkGray
        Write-Host "    Accounts → your account → 'Quick Install' tab → Generate." -ForegroundColor DarkGray
        return
    }

    $bridgeToken    = $claimResp.bridge_token
    $installationId = $claimResp.installation_id
    $accountLabel   = $claimResp.account_label
    $broker         = $claimResp.broker
    $accountNumber  = $claimResp.account_number
    $heartbeatUrl   = $claimResp.heartbeat_url
    $eaScriptUrl    = $claimResp.ea_script_url
    $eaLatestVer    = $claimResp.ea_latest_version

    Write-Host "    ✓ Paired with: $broker #$accountNumber  ($accountLabel)" -ForegroundColor Green
    if ($claimResp.device_key_id) { Write-Host "    ✓ Installer device key enrolled (key id $($claimResp.device_key_id))" -ForegroundColor Green }
    else { Write-Host "    ! server did not enrol the device key - live proof will not be attested (update the server)" -ForegroundColor Yellow }
    Write-Host ""

    # ── 3. Download latest EA source ──────────────────────────────────
    Write-Host "[3/5] Downloading EA source from $eaScriptUrl ..." -ForegroundColor Cyan
    $tmpMq5 = Join-Path $env:TEMP "EmergentTradingBridge.mq5"
    try {
        Invoke-WebRequest -Uri (Get-StoicEaDownloadUrl -BaseUrl $eaScriptUrl -Version $eaLatestVer) -OutFile $tmpMq5 -UseBasicParsing
    } catch {
        Write-Host "    ✗ Download failed: $($_.Exception.Message)" -ForegroundColor Red
        return
    }
    Write-Host "    ✓ Downloaded v$eaLatestVer ($(((Get-Item $tmpMq5).Length / 1KB).ToString('N0')) KB)" -ForegroundColor Green
    Write-Host ""

    # ── 4. Deploy EA + token into the terminal ────────────────────────
    Write-Host "[4/5] Deploying to the terminal ..." -ForegroundColor Cyan
    $installedTerminals = @()
    # N104-4 — the WebRequest allow-list is NOT terminal.ini (MT5 keeps it elsewhere and a running
    # terminal rewrites that file); the operator adds the URL by hand — printed in the summary.
    $heartbeatHost = Get-StoicWebRequestHost -HeartbeatUrl $heartbeatUrl
    foreach ($t in $terminals) {
        $expertsDir = Join-Path $t.FullName "MQL5\Experts"
        $filesDir   = Join-Path $t.FullName "MQL5\Files"

        # Copy EA source
        $destMq5 = Join-Path $expertsDir "EmergentTradingBridge.mq5"
        Copy-Item -Path $tmpMq5 -Destination $destMq5 -Force

        # Write bridge token (EA picks up from MQL5\Files at attach time)
        if (-not (Test-Path $filesDir)) { New-Item -ItemType Directory -Path $filesDir -Force | Out-Null }
        $tokenFile = Join-Path $filesDir "STOIC-Token.txt"
        @"
# STOIC bridge token — keep this file confidential.
# Account: $broker #$accountNumber ($accountLabel)
# Generated: $(Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
$bridgeToken
"@ | Set-Content -Path $tokenFile -Encoding UTF8 -NoNewline:$false

        # v1.55 — write the installation identity (EA sends it on every
        # heartbeat; the server rejects lease renewal without it).
        if ($installationId) {
            $instFile = Join-Path $filesDir "STOIC-Installation.txt"
            @"
# STOIC installation id — proves WHICH EA install is talking.
# Generated: $(Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
$installationId
"@ | Set-Content -Path $instFile -Encoding UTF8 -NoNewline:$false
        }

        # Compile via MetaEditor CLI (if found) — produces .ex5
        # v1.55: PREFER the CI-built EX5 published by the release pipeline —
        # download, verify SHA-256, report the digest back. Local MetaEditor
        # compilation is the FALLBACK only (server returns 409 when no CI
        # binary is published yet).
        $ciEx5Deployed = $false
        if (-not $NoCompile) {
            $ex5Dest = [System.IO.Path]::ChangeExtension($destMq5, ".ex5")
            try {
                $ex5Resp = Invoke-WebRequest -Uri "$ServerUrl/api/ea-script.ex5" -OutFile "$ex5Dest.tmp" -UseBasicParsing -PassThru
                $expectedHash = $ex5Resp.Headers["X-STOIC-SHA256"]
                $actualHash = (Get-FileHash "$ex5Dest.tmp" -Algorithm SHA256).Hash.ToLower()
                if ($expectedHash -and $actualHash -eq $expectedHash.ToLower()) {
                    Move-Item -Force "$ex5Dest.tmp" $ex5Dest
                    $ciEx5Deployed = $true
                    Write-Host "    ✓ $($t.Name)  →  CI-built .ex5 deployed (SHA-256 verified)" -ForegroundColor Green
                    # Report the deployed digest back to the server.
                    try {
                        $digestBody = @{ bridge_token = $bridgeToken; artifact = "stoic-ea-ex5"; sha256 = $actualHash; version = $eaLatestVer } | ConvertTo-Json
                        Invoke-RestMethod -Uri "$ServerUrl/api/infra/agent/artifact-digest" -Method Post -Body $digestBody -ContentType "application/json" | Out-Null
                    } catch {
                        Write-Host "    ⚠ digest report failed (non-fatal): $($_.Exception.Message)" -ForegroundColor Yellow
                    }
                } else {
                    Remove-Item -Force "$ex5Dest.tmp" -ErrorAction SilentlyContinue
                    Write-Host "    ✗ CI .ex5 hash mismatch (expected $expectedHash, got $actualHash) — REFUSING to install it; falling back to local compile" -ForegroundColor Red
                }
            } catch {
                Remove-Item -Force "$ex5Dest.tmp" -ErrorAction SilentlyContinue
                Write-Host "    • No CI-built .ex5 published yet — falling back to local MetaEditor compile" -ForegroundColor DarkGray
            }
        }
        if (-not $NoCompile -and -not $ciEx5Deployed) {
            $editor = $null
            # A15-2 — broker-branded installs ("IC Markets Global MT5", "Pepperstone MetaTrader 5", …)
            $candidates = @(
                "C:\Program Files\MetaTrader 5\metaeditor64.exe",
                "C:\Program Files (x86)\MetaTrader 5\metaeditor64.exe",
                "$env:LOCALAPPDATA\Programs\MetaTrader 5\metaeditor64.exe"
            )
            foreach ($root in @("C:\Program Files", "C:\Program Files (x86)", "$env:LOCALAPPDATA\Programs")) {
                if (Test-Path $root) {
                    $candidates += Get-ChildItem $root -Filter "metaeditor64.exe" -Recurse -Depth 1 -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
                }
            }
            # N104-4 — a portable terminal ships metaeditor64.exe in its OWN folder; origin.txt may be empty
            $candidates = @((Join-Path $t.FullName "metaeditor64.exe")) + $candidates
            $originDir = Get-StoicTerminalOrigin $t.FullName     # data folder → install folder of THIS terminal
            if ($originDir) { $candidates = @((Join-Path $originDir "metaeditor64.exe")) + $candidates }
            foreach ($candidate in $candidates) {
                if ($candidate -and (Test-Path $candidate)) { $editor = $candidate; break }
            }
            if ($editor) {
                $log = Join-Path $env:TEMP "stoic-compile.log"
                $ex5 = [System.IO.Path]::ChangeExtension($destMq5, ".ex5")
                # N105-5 — a stale .ex5 from an earlier build must never pass as "compiled": remove it first,
                # then trust the compile ONLY when MetaEditor's log reports 0 errors AND the file exists.
                Remove-Item -Force $ex5 -ErrorAction SilentlyContinue
                Remove-Item -Force $log -ErrorAction SilentlyContinue
                & $editor /compile:"$destMq5" /log:"$log" | Out-Null
                Start-Sleep -Milliseconds 600
                $compiled = Test-StoicCompileLog -LogPath $log
                if ($compiled -and (Test-Path $ex5)) {
                    Write-Host "    ✓ $($t.Name)  →  EA + token deployed, compiled .ex5 (0 errors)" -ForegroundColor Green
                } else {
                    Remove-Item -Force $ex5 -ErrorAction SilentlyContinue
                    Write-Host "    ⚠ $($t.Name)  →  EA + token deployed, .ex5 compile FAILED or reported errors (open MetaEditor, press F7, read the Errors tab; log: $log)" -ForegroundColor Yellow
                }
            } else {
                Write-Host "    ✓ $($t.Name)  →  EA + token deployed (no MetaEditor found; open it once, press F7 to compile)" -ForegroundColor Yellow
            }
        } else {
            Write-Host "    ✓ $($t.Name)  →  EA + token deployed (compile skipped)" -ForegroundColor Green
        }
        # r25 P1-01 / r26 P1-02 - binary proof: measure the EX5 that is ACTUALLY installed, drop the hash
        # where the EA can read it (MQL5\Files\STOIC-Proof.txt) and ATTEST it: the installer signs
        # {nonce, installation_id, terminal identity, EX5 hash, capabilities, ts} with its enrolled device
        # key. Live is admitted only when heartbeat == signed attestation == signed release.
        $finalEx5 = [System.IO.Path]::ChangeExtension($destMq5, ".ex5")
        if (Test-Path $finalEx5) {
            $proofHash = (Get-FileHash $finalEx5 -Algorithm SHA256).Hash.ToLower()
            Set-Content -Path (Join-Path $filesDir "STOIC-Proof.txt") -Value $proofHash -Encoding ASCII
            try {
                $att = Invoke-StoicAttestation -ServerUrl $ServerUrl -InstallationId $installationId `
                    -TerminalIdentity $t.Name -Ex5Sha256 $proofHash -Capabilities @("ex5_measured", "installer_v1_1", "dpapi_key")
                if ($att.release_match) { Write-Host "    + binary proof SIGNED and attested ($($proofHash.Substring(0,12))...) - matches the signed release" -ForegroundColor Green }
                else { Write-Host "    ! binary proof signed ($($proofHash.Substring(0,12))...) but does NOT match the signed release - live stays blocked until the release EX5 is installed" -ForegroundColor Yellow }
            } catch {
                $why = $_.Exception.Message
                if ($_.ErrorDetails -and $_.ErrorDetails.Message) { try { $why = ($_.ErrorDetails.Message | ConvertFrom-Json).detail.message } catch {} }
                Write-Host "    ! signed attestation failed: $why - re-run the installer; live stays blocked" -ForegroundColor Yellow
            }
            try {   # telemetry only (never proof): the plain digest report for the artifact matrix
                $digestBody = @{ bridge_token = $bridgeToken; installation_id = $installationId; artifact = "stoic-ea-ex5"; sha256 = $proofHash; version = $eaLatestVer; installer_version = $InstallerVersion; terminal = $t.Name } | ConvertTo-Json
                Invoke-RestMethod -Uri "$ServerUrl/api/infra/agent/artifact-digest" -Method Post -Body $digestBody -ContentType "application/json" | Out-Null
            } catch {}
        } else {
            Write-Host "    ! no .ex5 present - binary proof not recorded (compile in MetaEditor, then re-run the installer)" -ForegroundColor Yellow
        }
        $installedTerminals += $t.Name
    }
    Write-Host ""

    # ── 5. Summary ────────────────────────────────────────────────────
    Write-Host "[5/5] All set." -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Next steps (do once in MT5):" -ForegroundColor White
    Write-Host "  1. Open the MT5 terminal: $($terminal.FullName)" -ForegroundColor White
    Write-Host "  2. Tools → Options → Expert Advisors → tick 'Allow WebRequest for listed URL' and ADD:" -ForegroundColor White
    Write-Host "       $heartbeatHost" -ForegroundColor Yellow
    Write-Host "     (the installer cannot set this for you — MT5 keeps it outside terminal.ini)" -ForegroundColor DarkGray
    Write-Host "  3. Drag 'EmergentTradingBridge' from Navigator → Experts onto any chart (ServerUrl = $ServerUrl)." -ForegroundColor White
    Write-Host "  4. Confirm 'AutoTrading' is ON (top toolbar, green ▶)." -ForegroundColor White
    Write-Host ""
    Write-Host "  Your bridge token has been auto-saved at:" -ForegroundColor DarkGray
    Write-Host "    $(Join-Path $terminal.FullName 'MQL5\Files\STOIC-Token.txt')" -ForegroundColor DarkGray
    Write-Host "  The EA reads this automatically — no manual paste required." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "Deployed to $($installedTerminals.Count) terminal(s)." -ForegroundColor Green
    Write-Host ""
}
