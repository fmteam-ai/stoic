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

function Get-StoicRunningTerminal {
    # v1.5 — the MT5 the operator has OPEN right now: match terminal64.exe process folders against each data
    # folder's origin.txt (portable installs: data folder == exe folder). One match → that terminal, else $null.
    param([Parameter(Mandatory = $true)][array]$Candidates)
    $procs = @(Get-Process terminal64 -ErrorAction SilentlyContinue | ForEach-Object { try { Split-Path $_.Path -Parent } catch { $null } } | Where-Object { $_ })
    if ($procs.Count -eq 0) { return $null }
    $hits = @()
    foreach ($c in $Candidates) {
        $origin = Get-StoicTerminalOrigin $c.FullName
        foreach ($p in $procs) {
            if (($origin -and $origin.TrimEnd('\') -ieq $p.TrimEnd('\')) -or ($c.FullName.TrimEnd('\') -ieq $p.TrimEnd('\'))) { $hits += $c; break }
        }
    }
    $hits = @($hits | Sort-Object FullName -Unique)
    if ($hits.Count -eq 1) { return $hits[0] }
    return $null
}

function Get-StoicTerminalExe {
    # terminal64.exe for a data folder: origin.txt install dir, or the folder itself (portable mode). "" when unknown.
    param([Parameter(Mandatory = $true)][string]$DataFolder)
    foreach ($dir in @((Get-StoicTerminalOrigin $DataFolder), $DataFolder)) {
        if ($dir) { $exe = Join-Path $dir "terminal64.exe"; if (Test-Path $exe) { return $exe } }
    }
    return ""
}

function Restart-StoicTerminal {
    # v1.5 — close the running terminal of THIS install gracefully, relaunch with the startup config.
    # N110-3 — graceful close only (60 s); never force-kill a terminal that may be managing positions.
    param([Parameter(Mandatory = $true)][string]$Exe, [Parameter(Mandatory = $true)][string]$DataFolder,
          [Parameter(Mandatory = $true)][string]$StartupIni)
    $exeDir = (Split-Path $Exe -Parent).TrimEnd('\')
    $running = @(Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { try { (Split-Path $_.Path -Parent).TrimEnd('\') -ieq $exeDir } catch { $false } })
    foreach ($p in $running) {
        Write-Host "    closing MT5 (pid $($p.Id)) ..." -ForegroundColor DarkGray
        $null = $p.CloseMainWindow()
        if (-not $p.WaitForExit(60000)) {
            Write-Host "    ✗ MT5 did not close within 60 s — NOT force-killing it. Close it yourself, then start: `"$Exe`" /config:`"$StartupIni`"" -ForegroundColor Red
            return
        }
    }
    Start-Sleep -Seconds 2
    $args = @("/config:`"$StartupIni`"")
    if ($exeDir -ieq $DataFolder.TrimEnd('\')) { $args = @("/portable") + $args }
    Start-Process -FilePath $Exe -ArgumentList $args -WorkingDirectory $exeDir | Out-Null
    Write-Host "    MT5 restarted with the STOIC EA attached (Experts tab: 'STOIC Bridge EA v… started')." -ForegroundColor Green
}

function Get-StoicTerminalLogin {
    # N110-4 — the account the terminal is logged into right now, from its own journal: <data>\logs\YYYYMMDD.log
    # (UTF-16) carries "Network\t'12345678': authorized on Broker-Server through Access Point ..." (MT5 wording;
    # N111-3 — "login on" is the MT4 phrasing, both are accepted). "" when unknown. Newest log first, 7 days back.
    param([Parameter(Mandatory = $true)][string]$DataFolder)
    $logDir = Join-Path $DataFolder "logs"
    if (-not (Test-Path $logDir)) { return "" }
    # N111-3/CI — newest JOURNAL DAY first by file NAME (YYYYMMDD.log), not by mtime: a rotated or re-touched
    # older file must never outrank today's
    $logs = @(Get-ChildItem $logDir -Filter "*.log" -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 7)
    foreach ($log in $logs) {
        $text = ""
        try { $text = Get-Content $log.FullName -Encoding Unicode -Raw -ErrorAction Stop } catch { continue }
        $hits = [regex]::Matches($text, "'(\d{4,12})':\s*(?:authorized|login) on\s+(\S+)")
        if ($hits.Count -gt 0) { return $hits[$hits.Count - 1].Groups[1].Value }
    }
    return ""
}

function Get-StoicChartSymbol {
    # N110-3 — brokers suffix symbols (EURUSD.m, EURUSD.r): a startup ini with Symbol=EURUSD opens no chart and
    # the EA never attaches. Prefer the first OPEN chart whose symbol starts with $Preferred, then any open chart's
    # symbol, then $Preferred itself. N111-6 — only the ACTIVE profile is read (config\terminal.ini [Charts]
    # ProfileLast=…, else "Default"); other saved profiles are not what is on screen.
    param([Parameter(Mandatory = $true)][string]$DataFolder, [string]$Preferred = "EURUSD")
    $chartsDir = Join-Path $DataFolder "profiles\charts"
    $symbols = @()
    if (Test-Path $chartsDir) {
        $profile = "Default"
        $ini = Join-Path $DataFolder "config\terminal.ini"
        if (Test-Path $ini) {
            $iniText = ""
            try { $iniText = Get-Content $ini -Encoding Unicode -Raw -ErrorAction Stop } catch { $iniText = "" }
            if (-not $iniText -or $iniText -notmatch 'ProfileLast=') { try { $iniText = Get-Content $ini -Raw -ErrorAction Stop } catch { $iniText = "" } }
            $pm = [regex]::Match($iniText, '(?m)^\s*ProfileLast=(.+?)\s*$')
            if ($pm.Success -and $pm.Groups[1].Value.Trim()) { $profile = $pm.Groups[1].Value.Trim() }
        }
        $profileDir = Join-Path $chartsDir $profile
        if (-not (Test-Path $profileDir)) { $profileDir = $chartsDir }    # unknown profile name → every profile (best effort)
        foreach ($chr in @(Get-ChildItem $profileDir -Recurse -Filter "*.chr" -ErrorAction SilentlyContinue | Sort-Object FullName)) {
            $text = ""
            try { $text = Get-Content $chr.FullName -Encoding Unicode -Raw -ErrorAction Stop } catch { $text = "" }
            if (-not $text -or $text -notmatch 'symbol=') { try { $text = Get-Content $chr.FullName -Raw -ErrorAction Stop } catch { $text = "" } }
            $m = [regex]::Match($text, '(?m)^\s*symbol=([^\s]+)')
            if ($m.Success) { $symbols += $m.Groups[1].Value.Trim() }
        }
    }
    $pref = @($symbols | Where-Object { $_ -like "$Preferred*" } | Select-Object -First 1)
    if ($pref.Count -gt 0) { return $pref[0] }
    if ($symbols.Count -gt 0) { return $symbols[0] }
    return $Preferred
}

function Resolve-StoicTerminal {
    # ONE terminal: explicit path / id, the only one found, or the operator's choice. $null = nothing chosen.
    param([string]$TerminalPath, [string]$TerminalId,
          [string]$Root = (Join-Path $env:APPDATA "MetaQuotes\Terminal"),
          [string[]]$PortableRoots = @("C:\Program Files", "C:\Program Files (x86)", "$env:LOCALAPPDATA\Programs"),
          [scriptblock]$Prompt = { Read-Host "    terminal number (or re-run with -TerminalPath / -TerminalId)" },
          [scriptblock]$Confirm = { param($q) Read-Host $q })
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
    # v1.5 — several terminals: the one that is RUNNING is the one the operator is looking at.
    # N110-4 — but it may be logged into ANOTHER account than the pairing code's: show path + login, ask.
    $running = Get-StoicRunningTerminal -Candidates $found
    if ($running) {
        $login = Get-StoicTerminalLogin $running.FullName
        Write-Host "    MT5 terminal open right now: $($running.FullName)" -ForegroundColor Green
        Write-Host ("    logged in as: {0}" -f $(if ($login) { "#$login" } else { "(unknown — no login line in its log)" })) -ForegroundColor White
        $ok = & $Confirm "    Is this the terminal logged into the account this pairing code belongs to? [y/N]"
        if ("$ok" -match '^[Yy]') { return $running }
        Write-Host "    Not confirmed — choose the terminal below (or re-run with -TerminalPath / -TerminalId)." -ForegroundColor Yellow
    }
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
        [switch]$NoCompile,

        # v1.5 — zero-touch attach: the startup config opens this chart with the EA on it
        # (N110-3: when not given, the first open chart starting with EURUSD — broker suffixes included)
        [string]$ChartSymbol = "EURUSD",
        [string]$ChartPeriod = "M15",
        [switch]$NoRestart,
        # Phase 2 VPS Agent — the clone's journal is empty; the agent knows which login this terminal is FOR
        [string]$TerminalLogin = ""
    )

    $InstallerVersion = "1.7"
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
    $ServerUrl = $ServerUrl.TrimEnd('/')
    # N110-2 — the bridge token travels to this host: https only, no exceptions
    if ($ServerUrl -cnotmatch '^https://') { throw "STOIC: -ServerUrl must start with https:// (got '$ServerUrl') — the bridge token never travels in clear" }   # N111-6 — case-sensitive like the EA
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
    $terminalLogin = $(if ($TerminalLogin) { $TerminalLogin } else { Get-StoicTerminalLogin $terminal.FullName })
    Write-Host "    • $($terminal.FullName)" -ForegroundColor White
    if ($terminalLogin) { Write-Host "    • logged in as #$terminalLogin" -ForegroundColor White }
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
                terminal_login    = $(if ($terminalLogin) { $terminalLogin } else { $null })   # N111-2 — server checks it BEFORE consuming the code
                device_key        = @{ algorithm = "RSA-PSS-SHA256"; public_key = $devicePublicKey }
            } | ConvertTo-Json -Depth 4)
    } catch {
        $msg = ""; $code = ""
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            try { $d = ($_.ErrorDetails.Message | ConvertFrom-Json).detail; $msg = $d.message; $code = $d.code } catch {}
        }
        if (-not $msg) { $msg = $_.Exception.Message }
        Write-Host "    ✗ FAILED: $msg" -ForegroundColor Red
        Write-Host ""
        if ($code -eq "account_mismatch") {
            # N111-2 — nothing was consumed or rotated: the SAME code still works once the right account is logged in
            Write-Host "    This terminal is logged in as #$terminalLogin. Log the right account into MT5 (or re-run with" -ForegroundColor Yellow
            Write-Host "    -TerminalPath pointing at its data folder) and run the SAME line again — the code was not used." -ForegroundColor Yellow
            return
        }
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
    # N110-2 — the server URL the EA talks to is the one the server itself announced (same source as the
    # WebRequest host the user is told to allow-list); the -ServerUrl argument is only the bootstrap.
    # N111-2 — never abort AFTER the claim (the code is spent and the bridge token rotated): a non-https
    # announcement falls back to the https bootstrap URL with a warning instead.
    $eaServerUrl    = if ($claimResp.server_url) { "$($claimResp.server_url)".TrimEnd('/') } else { $ServerUrl }
    if ($eaServerUrl -cnotmatch '^https://') {
        Write-Host "    ! server announced a non-https server_url ($eaServerUrl) — using $ServerUrl instead (fix PUBLIC_BASE_URL on the server)" -ForegroundColor Yellow
        $eaServerUrl = $ServerUrl
    }

    Write-Host "    ✓ Paired with: $broker #$accountNumber  ($accountLabel)" -ForegroundColor Green
    # N110-4/N111-2 — the login/account comparison happened on the SERVER before the code was consumed
    # (terminal_login in the claim body → 409 account_mismatch with nothing changed); this is only a belt-and-braces echo.
    if ($terminalLogin -and $accountNumber -and ("$accountNumber" -ne "$terminalLogin")) {
        Write-Host "    ! server accepted the claim although this terminal reports login #$terminalLogin (code is for #$accountNumber) — update the server" -ForegroundColor Yellow
    }
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

        # v1.5 — server URL drop file: the EA auto-loads it, no inputs dialog edit needed
        @"
# STOIC server URL — the EA reads this when its ServerUrl input is left at default.
# Generated: $(Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
$eaServerUrl
"@ | Set-Content -Path (Join-Path $filesDir "STOIC-Server.txt") -Encoding UTF8 -NoNewline:$false

        # v1.5 — EA preset + MT5 startup config: terminal64.exe /config:<ini> opens a chart with the EA
        # attached and Algo Trading allowed, so nobody drags the EA or edits inputs.
        # N110-2 — the preset carries NO ServerUrl: the drop file is the single source (a later domain change
        # in STOIC-Server.txt must reach charts that loaded the preset, and the EA's https check must apply).
        $presetsDir = Join-Path $t.FullName "MQL5\Presets"
        if (-not (Test-Path $presetsDir)) { New-Item -ItemType Directory -Path $presetsDir -Force | Out-Null }
        "; STOIC preset (installer v$InstallerVersion) — no input overrides: server URL, token and installation id are read from MQL5\Files\STOIC-*.txt`r`n" | Set-Content -Path (Join-Path $presetsDir "stoic.set") -Encoding Unicode -NoNewline
        # N110-3 — EA-only startup: no terminal-wide DLL-import change (third-party EAs keep their DLLs);
        # the chart symbol follows the broker's suffix (EURUSD.m / EURUSD.r) when one is already open.
        $chartSymbol = if ($PSBoundParameters.ContainsKey('ChartSymbol')) { $ChartSymbol } else { Get-StoicChartSymbol -DataFolder $t.FullName -Preferred $ChartSymbol }
        $startupIni = Join-Path $t.FullName "stoic-start.ini"
        @"
; STOIC startup config (installer v$InstallerVersion) — launch: terminal64.exe /config:"$startupIni"
[Experts]
AllowLiveTrading=1
Enabled=1
Account=0
Profile=0
[StartUp]
Expert=EmergentTradingBridge
ExpertParameters=stoic.set
Symbol=$chartSymbol
Period=$ChartPeriod
"@ | Set-Content -Path $startupIni -Encoding ASCII

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
                $ex5Version   = "$($ex5Resp.Headers['X-STOIC-EA-Version'])".Trim()
                $actualHash = (Get-FileHash "$ex5Dest.tmp" -Algorithm SHA256).Hash.ToLower()
                if ($ex5Version -and $eaLatestVer -and $ex5Version -ne "$eaLatestVer") {
                    # N110-7 — a leftover older EX5 on the server must not be deployed as the latest version
                    Remove-Item -Force "$ex5Dest.tmp" -ErrorAction SilentlyContinue
                    Write-Host "    ✗ server's CI .ex5 is EA $ex5Version but the latest source is $eaLatestVer — not deploying it; compiling locally" -ForegroundColor Yellow
                } elseif ($expectedHash -and $actualHash -eq $expectedHash.ToLower()) {
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

    # ── 4b. Restart MT5 with the startup config (EA auto-attached) ───────
    if (-not $NoRestart -and $installedTerminals.Count -gt 0) {
        $exe = Get-StoicTerminalExe $terminal.FullName
        if ($exe) {
            # N110-3 — opt-in restart (default N) with the consequences spelled out first
            Write-Host "    MT5 can be restarted now so the EA attaches itself to a $chartSymbol chart." -ForegroundColor White
            Write-Host "    ⚠ While MT5 restarts, open positions stay open at the broker but are UNMANAGED (no stops moved, no closes)." -ForegroundColor Yellow
            Write-Host "    ⚠ The startup config turns Algo Trading ON for this terminal — every other EA on its charts becomes active too." -ForegroundColor Yellow
            Write-Host "    Say N if another EA is running or positions are open; attach EmergentTradingBridge by hand later instead." -ForegroundColor DarkGray
            $answer = Read-Host "    Restart MetaTrader 5 now? [y/N]"
            if ($answer -match '^[Yy]') { Restart-StoicTerminal -Exe $exe -DataFolder $terminal.FullName -StartupIni (Join-Path $terminal.FullName "stoic-start.ini") }
            else { Write-Host "    Not restarting. Later: `"$exe`" /config:`"$(Join-Path $terminal.FullName 'stoic-start.ini')`"  — or drag the EA onto a chart." -ForegroundColor DarkGray }
        } else {
            Write-Host "    (terminal64.exe not found for this data folder — attach the EA by hand, see below)" -ForegroundColor DarkGray
        }
    }

    # ── 5. Summary ────────────────────────────────────────────────────
    Write-Host "[5/5] All set." -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Two things left, in MT5 ($($terminal.FullName)):" -ForegroundColor White
    Write-Host "  1. Tools → Options → Expert Advisors → tick 'Allow WebRequest for listed URL' → add:" -ForegroundColor White
    Write-Host "       $heartbeatHost" -ForegroundColor Yellow
    Write-Host "     (MT5 keeps this list outside any file — the installer cannot set it for you)" -ForegroundColor DarkGray
    Write-Host "  2. If the EA is not on a chart after the restart: Navigator → Experts → drag 'EmergentTradingBridge' onto any chart → OK. AutoTrading ON (green ▶)." -ForegroundColor White
    Write-Host ""
    Write-Host "  Do NOT edit the EA inputs: server URL, bridge token and installation id are auto-loaded from" -ForegroundColor DarkGray
    Write-Host "    $(Join-Path $terminal.FullName 'MQL5\Files')\STOIC-*.txt" -ForegroundColor DarkGray
    Write-Host "  The dashboard (Accounts → Install Progress) turns green within a minute of the first heartbeat." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "Deployed to $($installedTerminals.Count) terminal(s)." -ForegroundColor Green
    Write-Host ""
}
