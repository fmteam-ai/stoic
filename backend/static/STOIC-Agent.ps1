# STOIC VPS Agent v1.3 - per-account PORTABLE MetaTrader 5 terminals, installed from the dashboard,
# kept alive by a watchdog (Easy-Connect Phase 2).
# ASCII-only on purpose (N113-2): Windows PowerShell 5.1 runs the scheduled task and reads a .ps1 as ANSI
# unless it carries a UTF-8 BOM; this file is served WITH a BOM and never contains a non-ASCII character.
# ============================================================================================
# ONE-TIME PREPARATION (per VPS, by you, ~5 minutes):
#   1. Install the broker's MT5 normally, then copy its program folder to  C:\STOIC\golden\MT5
#   2. Start  C:\STOIC\golden\MT5\terminal64.exe /portable  once, log in to ANY demo account,
#      Tools > Options > Expert Advisors > tick "Allow WebRequest for listed URL" > add your STOIC URL > OK > close MT5.
#      (That allow-list lives in the golden folder and is CLONED into every account terminal.)
#   3. Enrol the agent (Administrator PowerShell) with the ONE hash-pinned line from
#      Dashboard > VPS > Connect existing VPS (it downloads this script, verifies its SHA-256, then runs
#      Install-StoicAgent -ServerUrl ... -EnrollmentCode ... -ExpectedSha256 ...). Never paste an unpinned irm | iex.
#   4. For every MT5 account you will install, type its password ONCE on this VPS (DPAPI-encrypted, never sent anywhere):
#        Set-StoicTerminalLogin -Login 12345678 -Server "Broker-Demo"
# THEN: Dashboard > Accounts > account > Quick Install > "Install on my VPS". Nothing else to do on the VPS.
#
# WHAT THE AGENT DOES (scheduled task "StoicVpsAgent", runs at your logon, every 30 s):
#   * polls signed commands: install_terminal {login, server, pairing_token, server_url, chart_symbol, installer_sha256}
#       -> clone golden -> remove the golden account data -> run the public installer against the clone (EA, bridge token,
#          STOIC-Server.txt, stoic-start.ini) -> first start with [Login] from the DPAPI store (ini deleted afterwards)
#   * watchdog: terminal process dead -> start it; EA heartbeat stale (> 180 s, asked from the server) -> graceful restart;
#     at most 3 restarts per hour per terminal, then status "restart_loop" (the dashboard raises an ops alert)
#   * reports every terminal's state to /api/vps/agent/terminals/report (Install Progress shows it)
# Nothing here ever force-kills MT5 (CloseMainWindow + 60 s wait), and no password leaves this machine.
#
# RECOVERY (N113-6): if the dashboard shows commands stuck as "delivered" after a server database restore, the agent
# reports its last_seq in every heartbeat and in a "seq_replay" ack, and the server re-syncs its sequence by itself;
# just queue the command again. If vps-agent.json is unreadable (log: "agent not installed"), re-run the enrol line.
# ============================================================================================
param(
    [switch]$Run,
    [string]$ConfigPath = "$env:ProgramData\Stoic\vps-agent.json"
)

$script:AgentVersion = "1.3"
$script:TaskName = "StoicVpsAgent"
$script:Root = "C:\STOIC"
$script:DataDir = "$env:ProgramData\Stoic"
$script:GoldenDir = "C:\STOIC\golden\MT5"
$script:TerminalsDir = "C:\STOIC\MT5"
$script:LoginsDir = "$env:ProgramData\Stoic\logins"
$script:LogFile = "$env:ProgramData\Stoic\vps-agent.log"
$script:StaleS = 180
$script:MaxRestartsPerHour = 3
$script:Restarts = @{}          # login -> [datetime[]] restart stamps (last hour) - mirrored to disk (A17-7)
$script:StartedAt = @{}         # login -> last start time (A17-7 post-start grace)
$script:RestartLedger = "$env:ProgramData\Stoic\restarts.json"
$script:StartGraceS = 180
$script:LoginRe = '\A\d{4,12}\z'                       # N113-7 - \A \z: "$" would also match before a trailing newline
$script:TrustedOwners = @("BUILTIN\Administrators", "NT AUTHORITY\SYSTEM", "$($env:USERDOMAIN)\$($env:USERNAME)")
# audit #14 - compare on well-known SIDs, not on (localized) account names: Administrators, SYSTEM, the agent user
$script:TrustedSids = @("S-1-5-32-544", "S-1-5-18")
try { $script:TrustedSids += [Security.Principal.WindowsIdentity]::GetCurrent().User.Value } catch { }

try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

function Write-StoicLog([string]$Message, [string]$Level = "INFO") {
    $line = "{0} [{1}] {2}" -f (Get-Date).ToUniversalTime().ToString("o"), $Level, $Message
    try { New-Item -ItemType Directory -Force -Path (Split-Path $script:LogFile) | Out-Null; Add-Content -Path $script:LogFile -Value $line -Encoding UTF8 } catch { }
    if (-not $Run) { Write-Host $line }
}

# -- protected data folder (N113-3): %ProgramData% lets ANY local user pre-create folders/files they own; the elevated
#    task would then run a script or read a config that user can edit. Lock the folder down BEFORE the first write.
function ConvertTo-StoicSid($Identity) {
    # NTAccount / string -> SID string; $null when it cannot be translated (then the caller falls back to the name)
    try {
        if ($Identity -is [Security.Principal.SecurityIdentifier]) { return $Identity.Value }
        return ([Security.Principal.NTAccount]"$Identity").Translate([Security.Principal.SecurityIdentifier]).Value
    } catch { return $null }
}
function Test-StoicTrustedIdentity($Identity) {
    $sid = ConvertTo-StoicSid $Identity
    if ($sid) { return ($script:TrustedSids -contains $sid) }
    return ($script:TrustedOwners -contains "$Identity")
}
function Assert-StoicTrustedOwner([string]$Path) {
    $acl = Get-Acl -LiteralPath $Path
    $owner = $acl.Owner
    $sid = $null; try { $sid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value } catch { }
    $trusted = $(if ($sid) { $script:TrustedSids -contains $sid } else { Test-StoicTrustedIdentity $owner })
    if (-not $trusted) { throw "refusing to use '$Path': owned by '$owner' (expected Administrators, SYSTEM or $($env:USERDOMAIN)\$($env:USERNAME)) - delete it as Administrator and retry" }
}
function Invoke-StoicLockdown([string]$Path) {
    & icacls $Path /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "$($env:USERDOMAIN)\$($env:USERNAME):(OI)(CI)F" /T /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls on $Path returned $LASTEXITCODE - the folder could not be locked down (docs/VPS_AGENT.md); refusing to continue" }
}
function Test-StoicLockedDown([string]$Path) {
    # true when nothing outside Administrators / SYSTEM / the agent user has an access rule on the folder
    try {
        $acl = Get-Acl -LiteralPath $Path
        if (-not (Test-StoicTrustedIdentity $acl.Owner)) { return $false }
        foreach ($r in $acl.Access) { if (-not (Test-StoicTrustedIdentity $r.IdentityReference)) { return $false } }
        return $true
    } catch { return $false }
}
function Initialize-StoicDataDir {
    New-Item -ItemType Directory -Force -Path $script:DataDir | Out-Null
    Assert-StoicTrustedOwner $script:DataDir
    Invoke-StoicLockdown $script:DataDir
    foreach ($f in @(Get-ChildItem -LiteralPath $script:DataDir -Recurse -Force -ErrorAction SilentlyContinue)) {
        if ($f.Name -like "*.tmp") { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue; continue }
        Assert-StoicTrustedOwner $f.FullName
    }
    New-Item -ItemType Directory -Force -Path $script:LoginsDir | Out-Null
}

# -- config (agent token DPAPI-protected, current-user scope == the scheduled task's user) --------------------------
function Protect-StoicSecret([string]$Plain) { ConvertTo-SecureString $Plain -AsPlainText -Force | ConvertFrom-SecureString }
function Unprotect-StoicSecret([string]$Blob) {
    $ss = ConvertTo-SecureString $Blob
    [Runtime.InteropServices.Marshal]::PtrToStringUni([Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($ss))
}
function Get-StoicAgentConfig {
    if (-not (Test-Path $ConfigPath)) { throw "agent not installed - run the enrol line from Dashboard > VPS > Connect existing VPS" }
    Assert-StoicTrustedOwner $ConfigPath
    Get-Content $ConfigPath -Raw | ConvertFrom-Json
}
function Write-StoicFileAtomic([string]$Path, [string]$Content) {
    # N113-6 - write to a random temp file next to the target, then swap in place ([IO.File]::Replace keeps a crash from
    # leaving a half-written file behind); first write falls back to a move
    $tmp = Join-Path (Split-Path $Path) ("." + [IO.Path]::GetRandomFileName() + ".tmp")
    [IO.File]::WriteAllText($tmp, $Content, (New-Object Text.UTF8Encoding $false))
    if (Test-Path $Path) { [IO.File]::Replace($tmp, $Path, [NullString]::Value) } else { Move-Item -LiteralPath $tmp -Destination $Path -Force }
}
function Save-StoicAgentConfig($cfg) {
    New-Item -ItemType Directory -Force -Path (Split-Path $ConfigPath) | Out-Null
    Write-StoicFileAtomic $ConfigPath ($cfg | ConvertTo-Json -Depth 5)
}
function Invoke-StoicApi($cfg, [string]$Method, [string]$Path, $Body = $null) {
    $headers = @{}
    if ($cfg.cert_fingerprint) { $headers["X-Client-Cert-Fingerprint"] = $cfg.cert_fingerprint }
    $uri = "$($cfg.server_url)$Path"
    if ($null -ne $Body) {
        if ($Body -is [hashtable] -and $cfg.agent_token_enc) { $Body["agent_token"] = Unprotect-StoicSecret $cfg.agent_token_enc }
        return Invoke-RestMethod -Method $Method -Uri $uri -ContentType "application/json" -Headers $headers -Body ($Body | ConvertTo-Json -Depth 6) -TimeoutSec 30
    }
    Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -TimeoutSec 30
}

# -- enrolment -----------------------------------------------------------------------------------------------------
function Install-StoicAgent {
    param(
        [Parameter(Mandatory = $true)][string]$ServerUrl,
        [string]$EnrollmentCode = "",
        [string]$BootstrapToken = "",
        [string]$GoldenPath = "C:\STOIC\golden\MT5",
        # N112-1 - the enrol one-liner passes the SHA-256 it already verified; the persistent copy is checked against it
        [string]$ExpectedSha256 = ""
    )
    $ServerUrl = $ServerUrl.TrimEnd('/')
    if ($ServerUrl -cnotmatch '\Ahttps://') { throw "-ServerUrl must start with https://" }
    if (-not $EnrollmentCode -and -not $BootstrapToken) { throw "pass -EnrollmentCode (Dashboard > VPS > Connect existing VPS) or -BootstrapToken" }
    if (-not (Test-Path (Join-Path $GoldenPath "terminal64.exe"))) {
        Write-Host "  ! golden portable MT5 not found at $GoldenPath - prepare it first (see the header of this script). Enrolling anyway; installs will wait." -ForegroundColor Yellow
    }
    # N113-3 - lock %ProgramData%\Stoic down (and refuse a pre-created folder/file owned by someone else) BEFORE any write
    Initialize-StoicDataDir
    New-Item -ItemType Directory -Force -Path $script:Root, $script:TerminalsDir | Out-Null
    # A17-4 / N113-7 - C:\STOIC: Administrators, SYSTEM and the agent user only (inheritance off); other local users
    # must not read a first-start ini or swap a terminal64.exe the elevated agent launches. Hard failure, no warning.
    Assert-StoicTrustedOwner $script:Root
    Invoke-StoicLockdown $script:Root

    $facts = @{
        bootstrap_token = $BootstrapToken; enrollment_code = $EnrollmentCode
        agent_version = "vps-agent/$script:AgentVersion"; hostname = $env:COMPUTERNAME
        windows_version = (Get-CimInstance Win32_OperatingSystem).Caption
        machine_fingerprint = (Get-CimInstance Win32_ComputerSystemProduct).UUID
        timezone = (Get-TimeZone).Id
    }
    $reg = Invoke-RestMethod -Method POST -Uri "$ServerUrl/api/infra/agent/register" -ContentType "application/json" -Body ($facts | ConvertTo-Json) -TimeoutSec 30
    $cfg = [ordered]@{
        server_url = $ServerUrl; agent_id = $reg.agent_id
        agent_token_enc = Protect-StoicSecret $reg.agent_token
        command_key_enc = $(if ($reg.command_key) { Protect-StoicSecret $reg.command_key } else { $null })
        golden_path = $GoldenPath; terminals_dir = $script:TerminalsDir
        installed_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    Save-StoicAgentConfig $cfg

    # scheduled task at THIS user's logon (interactive session: terminals stay visible over RDP)
    # N112-1 - the file the task runs is either this very script or a copy verified against -ExpectedSha256
    $self = $PSCommandPath
    if (-not $self) {
        if (-not $ExpectedSha256) { throw "when run via iex, pass -ExpectedSha256 <sha of agent.ps1> (the dashboard one-liner does) so the persistent copy can be verified" }
        $target = Join-Path $script:DataDir "STOIC-Agent.ps1"
        # N113-3 - never reuse a pre-existing temp file (another local user may own it): random name, fresh file
        $tmp = Join-Path $script:DataDir ("." + [IO.Path]::GetRandomFileName() + ".tmp")
        $dl = Invoke-WebRequest -Uri "$ServerUrl/api/setup/agent.ps1" -UseBasicParsing
        [IO.File]::WriteAllBytes($tmp, $dl.RawContentStream.ToArray())
        $got = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
        if ($got -ne $ExpectedSha256.ToLower()) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue; throw "agent script hash mismatch ($got != $ExpectedSha256) - not installing" }
        if (Test-Path $target) { Assert-StoicTrustedOwner $target; Remove-Item -LiteralPath $target -Force }
        Move-Item -LiteralPath $tmp -Destination $target -Force
        Assert-StoicTrustedOwner $target
        $self = $target
    } elseif ($ExpectedSha256) {
        $got = (Get-FileHash $self -Algorithm SHA256).Hash.ToLower()
        if ($got -ne $ExpectedSha256.ToLower()) { throw "agent script hash mismatch ($got != $ExpectedSha256) - not installing" }
    }
    $action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$self`" -Run"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    Unregister-ScheduledTask -TaskName $script:TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $script:TaskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -User $env:USERNAME | Out-Null
    Start-ScheduledTask -TaskName $script:TaskName
    Write-Host "  OK  STOIC VPS Agent $($reg.agent_id) enrolled; task '$script:TaskName' running (log: $script:LogFile)" -ForegroundColor Green
    Write-Host "  Next: Set-StoicTerminalLogin -Login <mt5 login> -Server <broker server>  for each account, then 'Install on my VPS' in the dashboard." -ForegroundColor White
    # A17-8 / N113-7 - the task runs at LOGON: without auto-logon FOR THIS USER a reboot leaves no agent and no terminals
    $wl = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -ErrorAction SilentlyContinue
    $autoUser = "$($wl.DefaultUserName)"
    if ("$($wl.AutoAdminLogon)" -ne "1" -or $autoUser -ne $env:USERNAME) {
        Write-Host ""
        if ("$($wl.AutoAdminLogon)" -ne "1") { Write-Host "  !! REQUIRED: Windows auto-logon is OFF. After a reboot nothing would start until someone logs in via RDP." -ForegroundColor Yellow }
        else { Write-Host "  !! REQUIRED: Windows auto-logon is set for '$autoUser', but the agent task belongs to '$env:USERNAME'. After a reboot the agent would not start." -ForegroundColor Yellow }
        Write-Host "     Enable it for THIS user:  netplwiz  > untick 'Users must enter a user name and password' > OK > enter $env:USERNAME's password." -ForegroundColor Yellow
        Write-Host "     (or: Sysinternals Autologon.exe $env:USERNAME $env:USERDOMAIN <password>). The dashboard raises 'vps_agent_offline' when the agent is silent 5 min." -ForegroundColor Yellow
    }
}

# -- MT5 passwords: typed once here, DPAPI, never transmitted -------------------------------------------------------
function Test-StoicPassword([string]$Plain) {
    # N113-4 - MT5 reads "Password=<everything after the first =>", so = [ ] are FINE; only line breaks / NUL break the
    # ini, and non-ASCII would silently turn into "?" (the ini is written ASCII). Returns $null when acceptable.
    if ($Plain -match '[\r\n\x00]') { return "the password contains a line break or NUL character" }
    if ($Plain -cmatch '[^\x20-\x7E]') { return "the password contains non-ASCII characters (MT5's startup ini is ASCII) - change the MT5 password or log in by hand on the VPS" }
    return $null
}
function Set-StoicTerminalLogin {
    param([Parameter(Mandatory = $true)][string]$Login, [Parameter(Mandatory = $true)][string]$Server)
    if ($Login -cnotmatch $script:LoginRe) { throw "-Login must be the numeric MT5 account number (4-12 digits)" }
    $null = Assert-StoicIniValue "Server" $Server
    Initialize-StoicDataDir                                              # N113-3
    $pw = Read-Host "  MT5 password for #$Login on $Server (stored DPAPI-encrypted on THIS machine only)" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringUni([Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($pw))
    $why = Test-StoicPassword $plain
    if ($why) { throw "password not stored: $why" }
    $plain = $null
    $json = @{ login = $Login; server = $Server; password_enc = ($pw | ConvertFrom-SecureString); saved_at = (Get-Date).ToUniversalTime().ToString("o") } | ConvertTo-Json
    Write-StoicFileAtomic (Join-Path $script:LoginsDir "$Login.json") $json
    Write-Host "  OK  login #$Login stored for the agent's first start of that terminal" -ForegroundColor Green
}
function Get-StoicStoredLogin([string]$Login) {
    $p = Join-Path $script:LoginsDir "$Login.json"
    if (-not (Test-Path $p)) { return $null }
    Assert-StoicTrustedOwner $p
    $d = Get-Content $p -Raw | ConvertFrom-Json
    [pscustomobject]@{ login = $d.login; server = $d.server; password = (Unprotect-StoicSecret $d.password_enc) }
}

# -- terminals -----------------------------------------------------------------------------------------------------
function Assert-StoicLogin([string]$Login) {
    # A17-1 / N112-2 - the login becomes a folder name: digits only, always
    if ($Login -cnotmatch $script:LoginRe) { throw "refusing login '$Login' (must be 4-12 digits)" }
    return $Login
}
function Get-StoicTerminalDir([string]$Login) {
    # N112-2 - ALWAYS derived from the validated login; any server-sent directory is ignored
    $dir = [IO.Path]::GetFullPath((Join-Path $script:TerminalsDir "account-$(Assert-StoicLogin $Login)"))
    Assert-StoicUnderTerminals $dir
    return $dir
}
function Assert-StoicUnderTerminals([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath($script:TerminalsDir).TrimEnd('\') + '\'
    if ($full -like '\\*' -or $full -match '[*?]' -or ($full.Substring(2) -match ':')) { throw "refusing path '$Path' (UNC / wildcard / alternate stream)" }
    if (-not $full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw "refusing path '$Path' (outside $root)" }
    return $full
}
function Assert-StoicIniValue([string]$Name, [string]$Value) {
    # A17-1 / N112-4 - one gate for every ini KEY value (Login, Server): no line breaks, NUL, section/key syntax
    if ($Value -match '[\r\n\x00\[\]=]') { throw "refusing ini value for $Name (contains a line break, NUL, '[', ']' or '=')" }
    return $Value
}
function Read-StoicRestartLedger {
    if (-not (Test-Path $script:RestartLedger)) { return }
    try {
        $d = Get-Content $script:RestartLedger -Raw | ConvertFrom-Json
        foreach ($p in $d.PSObject.Properties) { $script:Restarts[$p.Name] = @($p.Value | ForEach-Object { [datetime]$_ }) }
    } catch { Write-StoicLog "restart ledger unreadable - starting fresh" "WARN" }
}
function Save-StoicRestartLedger {
    $out = @{}; $hour = (Get-Date).AddHours(-1)
    foreach ($k in $script:Restarts.Keys) { $out[$k] = @($script:Restarts[$k] | Where-Object { $_ -gt $hour } | ForEach-Object { $_.ToString("o") }) }
    try { Write-StoicFileAtomic $script:RestartLedger ($out | ConvertTo-Json -Depth 3) } catch { }
}
function Remove-StoicPasswordLeftovers {
    # A17-4 - a crash/reboot inside the 90 s window must never leave Password= on disk
    foreach ($f in @(Get-ChildItem $script:TerminalsDir -Recurse -Filter "stoic-first-start.ini" -ErrorAction SilentlyContinue)) {
        Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue; Write-StoicLog "removed leftover $($f.FullName)" "WARN"
    }
}
function Get-StoicTerminalProcess([string]$Dir) {
    $d = $Dir.TrimEnd('\')
    @(Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { try { (Split-Path $_.Path -Parent).TrimEnd('\') -ieq $d } catch { $false } })
}
function Stop-StoicTerminal([string]$Dir) {
    foreach ($p in (Get-StoicTerminalProcess $Dir)) {
        $null = $p.CloseMainWindow()
        if (-not $p.WaitForExit(60000)) { Write-StoicLog "terminal in $Dir did not close in 60 s - NOT force-killing" "WARN"; return $false }
    }
    return $true
}
function Start-StoicTerminal([string]$Dir, [string]$Ini) {
    $exe = Join-Path $Dir "terminal64.exe"
    Start-Process -FilePath $exe -ArgumentList @("/portable", "/config:`"$Ini`"") -WorkingDirectory $Dir -WindowStyle Minimized | Out-Null
}
function Send-StoicTerminalReport($cfg, [hashtable]$Report) {
    try { Invoke-StoicApi $cfg "POST" "/api/vps/agent/terminals/report" $Report | Out-Null } catch { Write-StoicLog "report failed: $($_.Exception.Message)" "WARN" }
}

function Invoke-StoicInstallTerminal($cfg, $params) {
    $login = Assert-StoicLogin "$($params.login)"; $dir = Get-StoicTerminalDir $login
    $base = @{ account_id = "$($params.account_id)"; login = $login; directory = $dir }
    if (-not $params.installer_sha256 -or "$($params.installer_sha256)" -notmatch '\A[0-9a-fA-F]{64}\z') {
        Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "command carries no installer_sha256 - refusing to run an unpinned installer (update the server)" })
        return $false
    }
    if (-not (Test-Path (Join-Path $cfg.golden_path "terminal64.exe"))) {
        Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "golden portable MT5 missing at $($cfg.golden_path) - prepare it on the VPS (see agent script header)" })
        return $false
    }
    Send-StoicTerminalReport $cfg ($base + @{ status = "installing"; detail = "cloning the golden terminal" })
    if (-not (Stop-StoicTerminal $dir)) { Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "an existing terminal in $dir would not close" }); return $false }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    # clone: everything but logs/, the golden account's saved logins and its open charts (fresh profile -> our startup ini decides)
    & robocopy $cfg.golden_path $dir /E /NFL /NDL /NJH /NJS /XD "$($cfg.golden_path)\logs" "$($cfg.golden_path)\MQL5\Logs" /XF "accounts.dat" "STOIC-*.txt" | Out-Null
    if ($LASTEXITCODE -ge 8) { Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "robocopy exit $LASTEXITCODE while cloning" }); return $false }
    Remove-Item (Join-Path $dir "config\accounts.dat") -Force -ErrorAction SilentlyContinue
    # A17-5 - an old token (re-install, or a golden folder that was ever paired) must never count as success
    Remove-Item (Join-Path $dir "MQL5\Files\STOIC-*.txt") -Force -ErrorAction SilentlyContinue
    $installStart = Get-Date

    # the public installer does the EA / token / server file / startup ini - exactly as a manual install would
    try {
        $src = Invoke-WebRequest -Uri "$($cfg.server_url)/api/setup/installer.ps1" -UseBasicParsing
        # A17-3 - the pin comes from the SIGNED command parameters, not from the same response
        $expected = "$($params.installer_sha256)".ToLower()
        $sha = (Get-FileHash -InputStream $src.RawContentStream -Algorithm SHA256).Hash.ToLower()
        if ($sha -ne $expected) { throw "installer hash mismatch ($sha != signed $expected)" }
        if ("$($params.server_url)" -cnotmatch '\Ahttps://') { throw "server_url must be https" }
        $symbol = "$($params.chart_symbol)"; if ($symbol -cnotmatch '\A[A-Za-z0-9._-]{1,24}\z') { throw "chart_symbol rejected" }
        Invoke-Expression ([Text.Encoding]::UTF8.GetString($src.RawContentStream.ToArray()).TrimStart([char]0xFEFF))
        Install-Stoic -Token "$($params.pairing_token)" -ServerUrl "$($params.server_url)" -TerminalPath $dir -NoRestart -ChartSymbol $symbol -TerminalLogin $login
    } catch {
        Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "installer: $($_.Exception.Message)" }); return $false
    }
    $tokenFile = Get-Item (Join-Path $dir "MQL5\Files\STOIC-Token.txt") -ErrorAction SilentlyContinue
    if (-not $tokenFile -or $tokenFile.LastWriteTime -lt $installStart) {
        Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "installer did not write a NEW bridge token (claim failed? see $script:LogFile)" }); return $false
    }

    # first start: [Login] from the DPAPI store if the operator typed it; the ini with the password is removed in finally.
    # N113-4 - anything wrong with the stored password / server / ACL falls back to a plain start (awaiting_login) instead
    # of failing the install; the operator logs in once by hand.
    $ini = Join-Path $dir "stoic-start.ini"
    $firstIni = Join-Path $dir "stoic-first-start.ini"
    $stored = $null; $skipWhy = $null
    try {
        try { $stored = Get-StoicStoredLogin $login } catch { $stored = $null; $skipWhy = "stored login unusable ($($_.Exception.Message))" }
        if ($stored) {
            # N112-4 - ONLY the server stored with the password; a differing signed parameter is a hard stop
            if ($params.server -and ("$($params.server)" -ne "$($stored.server)")) { throw "command server '$($params.server)' differs from the server stored with the password ('$($stored.server)') - refusing" }
            $null = Assert-StoicIniValue "Login" $login; $null = Assert-StoicIniValue "Server" $stored.server
            $why = Test-StoicPassword $stored.password
            if ($why) { $skipWhy = $why; $stored = $null }
            elseif (-not (Test-StoicLockedDown $script:Root)) { $skipWhy = "C:\STOIC is not locked down (icacls) - refusing to write a password file there"; $stored = $null }   # N113-7
        }
        if ($stored) {
            (Get-Content $ini -Raw) + "`r`n[Login]`r`nLogin=$login`r`nPassword=$($stored.password)`r`nServer=$($stored.server)`r`n" | Set-Content -Path $firstIni -Encoding ASCII
            Start-StoicTerminal $dir $firstIni
            Start-Sleep -Seconds 90
        } else {
            Start-StoicTerminal $dir $ini
        }
    } catch {
        Send-StoicTerminalReport $cfg ($base + @{ status = "failed"; detail = "first start: $($_.Exception.Message)" }); return $false
    } finally {
        Remove-Item $firstIni -Force -ErrorAction SilentlyContinue
    }
    $script:StartedAt[$login] = Get-Date
    $status = $(if ($stored) { "running" } else { "awaiting_login" })
    $detail = $(if ($stored) { "started with the stored login; waiting for the first EA heartbeat" }
                elseif ($skipWhy) { "started WITHOUT auto-login ($skipWhy): log #$login in once on the VPS" }
                else { "started - no stored password: log #$login in once on the VPS (or run Set-StoicTerminalLogin and re-install)" })
    Send-StoicTerminalReport $cfg ($base + @{ status = $status; detail = $detail; pid = (Get-StoicTerminalProcess $dir | Select-Object -First 1).Id; started_at = (Get-Date).ToUniversalTime().ToString("o") })
    return $true
}

function Invoke-StoicWatchdogTerminal($cfg, $t) {
    $login = "$($t.login)"
    if ($login -cnotmatch $script:LoginRe) { Write-StoicLog "status entry with invalid login '$login' ignored" "WARN"; return }
    $dir = Get-StoicTerminalDir $login          # N112-2 - never the server-sent directory
    if (-not (Test-Path (Join-Path $dir "terminal64.exe"))) { return }
    # N113-5 - never restart a terminal that is not expected to heartbeat yet (no login, still installing, queued)
    if ($t.status -in @("failed", "stopped", "restart_loop", "awaiting_login", "installing", "queued")) { return }
    $procs = Get-StoicTerminalProcess $dir
    $ini = Join-Path $dir "stoic-start.ini"
    $base = @{ account_id = "$($t.account_id)"; login = $login; directory = $dir }
    $hour = (Get-Date).AddHours(-1)
    $script:Restarts[$login] = @($script:Restarts[$login] | Where-Object { $_ -gt $hour })
    # A17-7 - post-start grace (local clock, survives a server that forgot started_at)
    if ($script:StartedAt[$login] -and ((Get-Date) - $script:StartedAt[$login]).TotalSeconds -lt $script:StartGraceS) { return }
    # A17-7 - local liveness: a running process whose Experts log advanced in the last 3 min is NOT stale, whatever
    # the server's heartbeat time says (heartbeat-ingest outage must not restart every terminal)
    $todayLog = Join-Path $dir ("MQL5\Logs\" + (Get-Date).ToString("yyyyMMdd") + ".log")
    $logFresh = (Test-Path $todayLog) -and (((Get-Date) - (Get-Item $todayLog).LastWriteTime).TotalSeconds -lt $script:StaleS)
    if ($procs.Count -eq 0) {
        if ($script:Restarts[$login].Count -ge $script:MaxRestartsPerHour) { Send-StoicTerminalReport $cfg ($base + @{ status = "restart_loop"; detail = "process keeps dying"; restarts_last_hour = $script:Restarts[$login].Count }); return }
        Write-StoicLog "terminal #$login not running - starting"
        Start-StoicTerminal $dir $ini; $script:Restarts[$login] += Get-Date; $script:StartedAt[$login] = Get-Date; Save-StoicRestartLedger
        Send-StoicTerminalReport $cfg ($base + @{ status = "restarted"; detail = "process was not running - started"; restarts_last_hour = $script:Restarts[$login].Count; started_at = (Get-Date).ToUniversalTime().ToString("o") })
    } elseif ($t.restart_wanted -and -not $logFresh) {
        if ($script:Restarts[$login].Count -ge $script:MaxRestartsPerHour) {
            Send-StoicTerminalReport $cfg ($base + @{ status = "restart_loop"; detail = "EA heartbeat still stale after $($script:Restarts[$login].Count) restarts this hour"; restarts_last_hour = $script:Restarts[$login].Count }); return
        }
        Write-StoicLog "terminal #$login heartbeat stale ($($t.heartbeat_age_s) s) - graceful restart"
        if (Stop-StoicTerminal $dir) {
            Start-Sleep -Seconds 3; Start-StoicTerminal $dir $ini; $script:Restarts[$login] += Get-Date; $script:StartedAt[$login] = Get-Date; Save-StoicRestartLedger
            Send-StoicTerminalReport $cfg ($base + @{ status = "restarted"; detail = "EA heartbeat stale - terminal restarted"; restarts_last_hour = $script:Restarts[$login].Count; started_at = (Get-Date).ToUniversalTime().ToString("o") })
        }
    }
}

function Invoke-StoicWatchdog($cfg) {
    $st = Invoke-StoicApi $cfg "POST" "/api/vps/agent/terminals/status" @{}
    foreach ($t in @($st.terminals)) {
        # N113-7 - one bad entry must not stop the cycle for the other terminals
        try { Invoke-StoicWatchdogTerminal $cfg $t } catch { Write-StoicLog "watchdog #$($t.login): $($_.Exception.Message)" "WARN" }
    }
    return @($st.terminals).Count
}

function ConvertTo-StoicCanonicalParams($Value) {
    # mirrors backend/vps_pathb.canonical_params: ORDINAL-sorted keys, "key`0value`n", $null -> "", bool -> true/false,
    # scalars as-is; nested objects -> compact sorted JSON (not used by vps-agent commands). No JSON string escaping
    # anywhere, so PowerShell 5.1 / 7 and Python agree byte-for-byte.
    if ($null -eq $Value) { return "" }
    $pairs = @{}
    if ($Value -is [hashtable]) { foreach ($k in $Value.Keys) { $pairs[[string]$k] = $Value[$k] } }
    else { foreach ($p in $Value.PSObject.Properties) { $pairs[$p.Name] = $p.Value } }
    [string[]]$keys = @($pairs.Keys)
    [Array]::Sort($keys, [StringComparer]::Ordinal)
    $sb = New-Object System.Text.StringBuilder
    foreach ($k in $keys) {
        $v = $pairs[$k]
        $s = ""
        if ($null -eq $v) { $s = "" }
        elseif ($v -is [bool]) { $s = $(if ($v) { "true" } else { "false" }) }
        elseif ($v -is [array] -or $v -is [pscustomobject] -or $v -is [hashtable]) { $s = ($v | ConvertTo-Json -Compress -Depth 8) }
        else { $s = "$v" }
        [void]$sb.Append($k).Append([char]0).Append($s).Append("`n")
    }
    return $sb.ToString()
}

function Test-StoicFixedTimeEqual([string]$A, [string]$B) {
    if ($A.Length -ne $B.Length) { return $false }
    $diff = 0; for ($i = 0; $i -lt $A.Length; $i++) { $diff = $diff -bor ([int][char]$A[$i] -bxor [int][char]$B[$i]) }
    return ($diff -eq 0)
}

function Get-StoicLastSeq($cfg) { if ($cfg.last_seq) { [int64]$cfg.last_seq } else { [int64]0 } }

function Invoke-StoicCommands($cfg) {
    $resp = Invoke-StoicApi $cfg "POST" "/api/infra/agent/commands/poll" @{}
    foreach ($c in @($resp.commands)) {
        # signed command sequence (iter-122 P3 + audit #12 P3): HMAC(agent_id|command_id|seq|command|sha256(params))
        # with the enrolment command_key - the PARAMS (pairing token, server URL, directory) are bound too
        if (-not $cfg.command_key_enc) { Write-StoicLog "no command key in the agent config - refusing ALL commands (re-enrol)" "WARN"; break }   # A17-9 fail closed
        $lastSeq = Get-StoicLastSeq $cfg
        if ($null -eq $c.seq -or [int64]$c.seq -le $lastSeq) {
            # N113-6 - tell the server (it re-syncs its sequence from last_seq) instead of leaving the command "delivered" forever
            Write-StoicLog "command $($c.command_id) seq $($c.seq) not greater than last seen $lastSeq - replay ignored, server told" "WARN"
            try { Invoke-StoicApi $cfg "POST" "/api/infra/agent/commands/ack" @{ command_id = $c.command_id; ok = $false; detail = "seq_replay: agent last_seq=$lastSeq"; last_seq = $lastSeq } | Out-Null } catch { Write-StoicLog "replay ack failed: $($_.Exception.Message)" "WARN" }
            continue
        }
        if (-not $c.sig_v2) { Write-StoicLog "command $($c.command_id) has no sig_v2 (server too old?) - ignored" "WARN"; continue }
        $key = [Text.Encoding]::UTF8.GetBytes((Unprotect-StoicSecret $cfg.command_key_enc))
        $h = New-Object System.Security.Cryptography.HMACSHA256 (,$key)
        $paramsCanon = ConvertTo-StoicCanonicalParams $c.params
        $paramsHash = ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($paramsCanon))) -replace '-', '').ToLower()
        $calc = ([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes("$($cfg.agent_id)|$($c.command_id)|$($c.seq)|$($c.command)|$paramsHash"))) -replace '-', '').ToLower()
        if (-not (Test-StoicFixedTimeEqual $calc "$($c.sig_v2)".ToLower())) { Write-StoicLog "command $($c.command_id) signature mismatch - ignored" "WARN"; continue }
        $cfg | Add-Member -NotePropertyName last_seq -NotePropertyValue ([int64]$c.seq) -Force; Save-StoicAgentConfig $cfg
        $ok = $false; $detail = ""
        try {
            switch ($c.command) {
                "install_terminal" { $ok = Invoke-StoicInstallTerminal $cfg $c.params; $detail = $(if ($ok) { "installed" } else { "see terminal report" }) }
                "restart_terminal" {
                    $login = Assert-StoicLogin "$($c.params.login)"; $dir = Get-StoicTerminalDir $login   # N112-2 - server directory ignored
                    $base = @{ account_id = "$($c.params.account_id)"; login = $login; directory = $dir }
                    $ok = (Stop-StoicTerminal $dir)
                    if ($ok) { Start-Sleep -Seconds 3; Start-StoicTerminal $dir (Join-Path $dir "stoic-start.ini"); $script:StartedAt[$login] = Get-Date; $detail = "restarted" }
                    else { $detail = "terminal would not close within 60 s - not force-killed" }
                    if ($login) { Send-StoicTerminalReport $cfg ($base + @{ status = $(if ($ok) { "restarted" } else { "running" }); detail = "dashboard restart: $detail"; pid = (Get-StoicTerminalProcess $dir | Select-Object -First 1).Id }) }
                }
                "run_diagnostics" { $ok = $true; $detail = "terminals=" + ((Get-ChildItem $script:TerminalsDir -Directory -ErrorAction SilentlyContinue | Measure-Object).Count) }
                default { $ok = $false; $detail = "unsupported by vps-agent" }
            }
        } catch { $ok = $false; $detail = $_.Exception.Message }
        try { Invoke-StoicApi $cfg "POST" "/api/infra/agent/commands/ack" @{ command_id = $c.command_id; ok = $ok; detail = "$detail"; last_seq = (Get-StoicLastSeq $cfg) } | Out-Null } catch { Write-StoicLog "ack failed: $($_.Exception.Message)" "WARN" }
    }
}

function Send-StoicHeartbeat($cfg, [int]$Managed) {
    $running = @(Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { try { $_.Path -like "$($script:TerminalsDir)\*" } catch { $false } }).Count
    $os = Get-CimInstance Win32_OperatingSystem
    $disk = Get-PSDrive -Name C
    $metrics = @{
        agent_version = "vps-agent/$script:AgentVersion"; hostname = $env:COMPUTERNAME
        golden_ready = (Test-Path (Join-Path $cfg.golden_path "terminal64.exe"))
        terminals_managed = $Managed; terminals_running = $running; mt5_processes = $running
        disk_free_gb = [math]::Round($disk.Free / 1GB, 1)
        ram_percent = [math]::Round((1 - ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize)) * 100, 0)
        last_seq = (Get-StoicLastSeq $cfg)                                   # N113-6 - lets the server re-sync after a DB restore
    }
    try { Invoke-StoicApi $cfg "POST" "/api/infra/agent/heartbeat" @{ metrics = $metrics } | Out-Null } catch { Write-StoicLog "heartbeat failed: $($_.Exception.Message)" "WARN" }
}

function Start-StoicAgentLoop {
    Initialize-StoicDataDir            # N113-3 - refuse a data folder / file another local user owns
    $cfg = Get-StoicAgentConfig
    Write-StoicLog "STOIC VPS Agent $script:AgentVersion started ($($cfg.agent_id))"
    Remove-StoicPasswordLeftovers      # A17-4
    Read-StoicRestartLedger            # A17-7 - the restart budget survives an agent restart
    while ($true) {
        $managed = 0
        try { Invoke-StoicCommands $cfg } catch { Write-StoicLog "commands: $($_.Exception.Message)" "WARN" }
        try { $managed = Invoke-StoicWatchdog $cfg } catch { Write-StoicLog "watchdog: $($_.Exception.Message)" "WARN" }
        Send-StoicHeartbeat $cfg $managed
        Start-Sleep -Seconds 30
    }
}

if ($Run) { Start-StoicAgentLoop }
