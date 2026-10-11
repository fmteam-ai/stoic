# M122 — live onboarding fixes (OnEquity demo #1083440) · EA 1.63 · installer v1.8

Found during live onboarding on the VPS (installer v1.7, EA 1.62). All four are in this release.

## P1 — every fresh Windows install was rejected (401 "Invalid bridge token")
**Root cause.** `STOIC-Installer.ps1` wrote `STOIC-Token.txt` / `STOIC-Installation.txt` / `STOIC-Server.txt` with
`Set-Content -Encoding UTF8`. Windows PowerShell **5.1** writes a UTF-8 **BOM** (pwsh 7 on the Linux CI runner does not,
so CI never saw it). The EA opens the files with `FILE_TXT|FILE_ANSI`: the three BOM bytes became `ï»¿` in front of
the first comment line, the `'#'` test failed and the EA sent
`# STOIC bridge token — keep this file confidential.` as the token (EA log `length=56`; a real token is 43) → 401 on
every heartbeat / poll-trades. `STOIC-Server.txt` worked only because that reader searches for `https://`.

**Fixes (both sides + CI).**
- Installer v1.8: `Write-StoicDropFile` → `[IO.File]::WriteAllText(path, text, UTF8Encoding($false))` for every
  `STOIC-*.txt` (Proof.txt stays `-Encoding ASCII`, which has no BOM). `Test-StoicDropFileNoBom` helper.
- EA 1.63: `StripBom()` (U+FEFF, or up to three leading chars ≥ 0x80 — the BOM read as ANSI) runs in **every** drop-file
  reader through `ReadDropFileValue()`; the shape of each value is validated before anything is sent:
  token `^[A-Za-z0-9_-]{32,128}$` (a range, so a future token format does not brick every EA), installation
  `^inst_[0-9a-f]{12}$`, proof 64 hex. A bad file prints `STOIC: token file malformed … Heartbeats NOT sent`
  (resp. `installation file malformed`) instead of a 401 storm. The pasted-inputs path is validated the same way.
  A 1.63 EA still **recovers** the token from an old BOM file, so terminals installed with v1.7 heal on EA update alone.
- CI (`ea-compile` job, windows-latest): `scripts/test_installer_dropfiles.ps1` runs under **powershell.exe 5.1**
  (not pwsh), writes the three files through the installer's writer, asserts no `EF BB BF`, parses them with
  `scripts/ea_dropfile_reader.py` — a line-for-line Python port of the EA readers (`--legacy` reproduces the ≤ 1.62
  reader) — and runs a negative control: the pre-1.8 writer + legacy reader must yield the comment line.
- Host workaround already applied on our VPS (rewrite the four files without BOM, re-attach the EA) stays valid.

## P2 — the broker server of an account could not be corrected
- `PATCH /api/accounts/{id}` accepts `server` (exact MT5 name, `^[A-Za-z0-9][A-Za-z0-9 ._-]{1,63}$`): **step-up MFA**
  (`account_server_edit`), audited (`account_server_edited`), updates `server` / `broker_server` /
  `expected_identity.broker_server` together, clears `verified_identity` + `broker_account_mismatch` so the chain
  re-verifies on the next heartbeat. Paper accounts refused.
- `POST /api/accounts/{id}/adopt-reported-server`: when the token-authenticated EA reports a different server for the
  **same** MT5 login, adopt what the EA reports (step-up, audited `account_server_adopted_from_ea`; alias-equal → no-op;
  a different login → 409 `login_differs`). Quick Install shows **"Use the server the EA reports (X)"** and
  **"Edit server by hand"** (`ServerMismatchFix.jsx`).
- `GET /api/accounts/server-suggest?server=&broker=`: known / suggestions / hint from the broker registry (presets +
  alias groups + `broker_profiles`). The Add-account wizard validates the typed name live, offers the exact names as
  chips, and has "Other — type the exact MT5 server name" even when presets exist. Registry: `OnEquity-Server` added
  (what MT5 reports for the OnEquity demo).

## P2 — diagnostics that sent the operator the wrong way
1. Install Progress: a 401 heartbeat is recorded on the account row(s) of the reported MT5 login
   (`bridge_rejected {at, reason, token_len, ea_version, broker_server, ip}` — no secret material, 1 write / login / 30 s,
   cleared by the next accepted heartbeat). The heartbeat step then reads **"Heartbeats arriving but REJECTED: invalid
   bridge token (401)"** with a BOM-specific hint (suspended / retired tokens get their own hints) instead of
   "Most likely the WebRequest URL is missing".
2. EA `HttpPost`: non-2xx answers print `STOIC: <endpoint> rejected HTTP <status> <server code> — <hint>`, one line per
   minute per status (`LogHttpRejected`), e.g. `STOIC: heartbeat rejected HTTP 401 invalid_bridge_token — …`.
3. The identity step no longer suggests "Re-pair (Quick Install)" for a *broker server mismatch* (points at the fix
   above); the Accounts banner says **BROKER SERVER MISMATCH** with the right text instead of "Two EAs attached to the
   same terminal will mirror the same balance".
4. Backend: `bridge 401 reason=unknown|retired|suspended account=<id|-> token_len=<n> token_prefix=<4>` at INFO; the 401
   body now carries a code (`invalid_bridge_token` / `bridge_token_retired` / `bridge_token_suspended`), message unchanged.

## P3 — one canonical host
EA compiled default `ServerUrl` / `SERVER_URL_DEFAULT` = **`https://stoicaibot.com`** (= installer `-ServerUrl` =
panel WebRequest line). `scripts/live_probe.py` / `spike_webrequest_startup.ps1` defaults follow; `deploy_watch`
keeps accepting both hosts.

## Release order (operator)
1. Save to GitHub → CI green (unit lane incl. `test_main122_onboarding.py`, drift guard re-captured for 1.63, new 5.1
   drop-file gate, MetaEditor compile).
2. The MQ5 push triggers **ea-release** (MetaEditor compile → external signer, key `stoic-release-ed25519-v2`) which
   commits `release/ea_release.json` + `docs/RELEASE_HASHES.json#ea.ex5_sha256`. **The new EX5 hash is produced there**
   (`python scripts/verify_ea_release.py --check` prints it); it cannot be computed outside MetaEditor.
3. Tag (e.g. `v1.60.16`), wait for the Release workflow, `deploy/update.sh` on the VPS.
4. Terminals: re-run the installer (v1.8 is served by the updated server) or just install the 1.63 EX5 — it reads the
   old BOM files correctly.
