# STOIC-Installer.ps1 — one-shot MetaTrader 5 EA installer for STOIC
# ============================================================================
# Usage on a Windows MT5 host (your PC or broker VPS):
#
#   irm https://your-stoic-host/api/setup/installer.ps1 | iex
#   Install-Stoic -Token "abc123..." -ServerUrl "https://your-stoic-host"
#
# OR download + run locally:
#
#   .\STOIC-Installer.ps1 -Token "abc123..." -ServerUrl "https://your-stoic-host"
#
# What it does:
#   1. Calls /api/setup/claim-pairing with your token, gets back the
#      bridge_token + heartbeat URL.
#   2. Auto-discovers every MT5 terminal data folder (%APPDATA%\MetaQuotes\...).
#   3. Downloads the latest EmergentTradingBridge.mq5 from the server.
#   4. Copies it into each terminal's MQL5\Experts\ folder.
#   5. Writes the bridge token into MQL5\Files\STOIC-Token.txt — the EA
#      reads it on attach if no token is hard-coded.
#   6. Whitelists the STOIC URL in terminal.ini (WebRequest allow-list).
#   7. Invokes metaeditor64.exe to compile (when found) — produces .ex5
#      next to the .mq5 ready for chart attach.
#
# After running, the user only needs to:
#   - Open MT5, drag the EmergentTradingBridge EA onto any chart.
#   - Confirm AutoTrading is ON (green play button).
#
# The bridge will pick up the token from MQL5\Files\STOIC-Token.txt
# automatically (no manual paste).
# ============================================================================

function Install-Stoic {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Token,

        [Parameter(Mandatory = $true)]
        [string]$ServerUrl,

        [string]$Hostname = $env:COMPUTERNAME,

        [switch]$NoCompile
    )

    $InstallerVersion = "1.0"
    $ServerUrl = $ServerUrl.TrimEnd('/')

    Write-Host ""
    Write-Host "===========================================" -ForegroundColor Yellow
    Write-Host " STOIC · MT5 BRIDGE AUTO-INSTALLER  v$InstallerVersion" -ForegroundColor Yellow
    Write-Host "===========================================" -ForegroundColor Yellow
    Write-Host ""

    # ── 1. Claim pairing token ────────────────────────────────────────
    Write-Host "[1/5] Claiming pairing token at $ServerUrl ..." -ForegroundColor Cyan
    try {
        $claimResp = Invoke-RestMethod `
            -Method POST `
            -Uri "$ServerUrl/api/setup/claim-pairing" `
            -ContentType "application/json" `
            -Body (@{
                token             = $Token
                hostname          = $Hostname
                installer_version = $InstallerVersion
            } | ConvertTo-Json)
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
    Write-Host ""

    # ── 2. Discover MT5 terminals ─────────────────────────────────────
    Write-Host "[2/5] Discovering MetaTrader 5 installations ..." -ForegroundColor Cyan
    $mtRoot = Join-Path $env:APPDATA "MetaQuotes\Terminal"
    if (-not (Test-Path $mtRoot)) {
        Write-Host "    ✗ No MetaTrader installations found under $mtRoot" -ForegroundColor Red
        Write-Host "    Install MT5 from your broker first, then re-run." -ForegroundColor DarkGray
        return
    }
    # Terminal data folders have a 32-char hex GUID name (skip Community/Help/etc).
    $terminals = Get-ChildItem $mtRoot -Directory | Where-Object {
        $_.Name -match '^[0-9A-F]{32}$' -and
        (Test-Path (Join-Path $_.FullName "MQL5\Experts"))
    }
    if ($terminals.Count -eq 0) {
        Write-Host "    ✗ Found $mtRoot but no terminals with MQL5\Experts inside." -ForegroundColor Red
        return
    }
    foreach ($t in $terminals) {
        Write-Host "    • $($t.FullName)" -ForegroundColor White
    }
    Write-Host ""

    # ── 3. Download latest EA source ──────────────────────────────────
    Write-Host "[3/5] Downloading EA source from $eaScriptUrl ..." -ForegroundColor Cyan
    $tmpMq5 = Join-Path $env:TEMP "EmergentTradingBridge.mq5"
    try {
        Invoke-WebRequest -Uri "$eaScriptUrl?v=$eaLatestVer" -OutFile $tmpMq5 -UseBasicParsing
    } catch {
        Write-Host "    ✗ Download failed: $($_.Exception.Message)" -ForegroundColor Red
        return
    }
    Write-Host "    ✓ Downloaded v$eaLatestVer ($(((Get-Item $tmpMq5).Length / 1KB).ToString('N0')) KB)" -ForegroundColor Green
    Write-Host ""

    # ── 4. Deploy EA + token + URL whitelist into each terminal ───────
    Write-Host "[4/5] Deploying to terminals ..." -ForegroundColor Cyan
    $installedTerminals = @()
    foreach ($t in $terminals) {
        $expertsDir = Join-Path $t.FullName "MQL5\Experts"
        $filesDir   = Join-Path $t.FullName "MQL5\Files"
        $iniPath    = Join-Path $t.FullName "config\terminal.ini"

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

        # Whitelist heartbeat URL in terminal.ini (WebRequest allow-list)
        $heartbeatHost = ([Uri]$heartbeatUrl).Scheme + "://" + ([Uri]$heartbeatUrl).Authority
        if (Test-Path $iniPath) {
            $iniText = Get-Content $iniPath -Raw
            if ($iniText -notmatch [regex]::Escape($heartbeatHost)) {
                # Append to [Experts] section, or create it.
                if ($iniText -match '\[Experts\]') {
                    $iniText = $iniText -replace '\[Experts\]', "[Experts]`r`nWebRequest=$heartbeatHost"
                } else {
                    $iniText += "`r`n[Experts]`r`nWebRequest=$heartbeatHost`r`n"
                }
                Set-Content -Path $iniPath -Value $iniText -Encoding UTF8
            }
        } else {
            $iniDir = Split-Path $iniPath -Parent
            if (-not (Test-Path $iniDir)) { New-Item -ItemType Directory -Path $iniDir -Force | Out-Null }
            "[Experts]`r`nWebRequest=$heartbeatHost`r`n" | Set-Content -Path $iniPath -Encoding UTF8
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
            foreach ($candidate in @(
                "C:\Program Files\MetaTrader 5\metaeditor64.exe",
                "C:\Program Files (x86)\MetaTrader 5\metaeditor64.exe",
                "$env:LOCALAPPDATA\Programs\MetaTrader 5\metaeditor64.exe"
            )) {
                if (Test-Path $candidate) { $editor = $candidate; break }
            }
            if ($editor) {
                $log = Join-Path $env:TEMP "stoic-compile.log"
                & $editor /compile:"$destMq5" /log:"$log" | Out-Null
                Start-Sleep -Milliseconds 600
                $ex5 = [System.IO.Path]::ChangeExtension($destMq5, ".ex5")
                if (Test-Path $ex5) {
                    Write-Host "    ✓ $($t.Name)  →  EA + token deployed, compiled .ex5" -ForegroundColor Green
                } else {
                    Write-Host "    ⚠ $($t.Name)  →  EA + token deployed, .ex5 compile may have failed (open MetaEditor and press F7 manually)" -ForegroundColor Yellow
                }
            } else {
                Write-Host "    ✓ $($t.Name)  →  EA + token deployed (no MetaEditor found; open it once, press F7 to compile)" -ForegroundColor Yellow
            }
        } else {
            Write-Host "    ✓ $($t.Name)  →  EA + token deployed (compile skipped)" -ForegroundColor Green
        }
        # r25 P1-01 - binary proof: measure the EX5 that is ACTUALLY installed, drop the hash
        # where the EA can read it (MQL5\Files\STOIC-Proof.txt) and record it server-side bound
        # to this installation. Live is admitted only when heartbeat == installer record == signed release.
        $finalEx5 = [System.IO.Path]::ChangeExtension($destMq5, ".ex5")
        if (Test-Path $finalEx5) {
            $proofHash = (Get-FileHash $finalEx5 -Algorithm SHA256).Hash.ToLower()
            Set-Content -Path (Join-Path $filesDir "STOIC-Proof.txt") -Value $proofHash -Encoding ASCII
            try {
                $proofBody = @{ bridge_token = $bridgeToken; installation_id = $installationId; artifact = "stoic-ea-ex5"; sha256 = $proofHash; version = $eaLatestVer; installer_version = $InstallerVersion; terminal = $t.Name } | ConvertTo-Json
                $proofResp = Invoke-RestMethod -Uri "$ServerUrl/api/infra/agent/artifact-digest" -Method Post -Body $proofBody -ContentType "application/json"
                if ($proofResp.match) { Write-Host "    + binary proof recorded ($($proofHash.Substring(0,12))...) - matches the signed release" -ForegroundColor Green }
                else { Write-Host "    ! binary proof recorded ($($proofHash.Substring(0,12))...) but does NOT match the signed release - live stays blocked until the release EX5 is installed" -ForegroundColor Yellow }
            } catch {
                Write-Host "    ! binary proof report failed: $($_.Exception.Message) - re-run the installer; live stays blocked" -ForegroundColor Yellow
            }
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
    Write-Host "  1. Open the MT5 terminal." -ForegroundColor White
    Write-Host "  2. Drag 'EmergentTradingBridge' from Navigator → Experts onto any chart." -ForegroundColor White
    Write-Host "  3. Confirm 'AutoTrading' is ON (top toolbar, green ▶)." -ForegroundColor White
    Write-Host ""
    Write-Host "  Your bridge token has been auto-saved at:" -ForegroundColor DarkGray
    Write-Host "    %APPDATA%\MetaQuotes\Terminal\<terminal-id>\MQL5\Files\STOIC-Token.txt" -ForegroundColor DarkGray
    Write-Host "  The EA reads this automatically — no manual paste required." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "Deployed to $($installedTerminals.Count) terminal(s)." -ForegroundColor Green
    Write-Host ""
}
