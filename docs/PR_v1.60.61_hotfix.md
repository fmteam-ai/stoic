# PR: v1.60.61 hotfix — EA release chain must fail loudly (run 39030924128 / commit 585d2ce9)

Branch `hotfix/v1.60.61-ea-release-chain` (Save to GitHub), base `main` (PR #112 merged). **No trading code, no MQ5,
no `requirements.txt`, no protected file touched** — `docs/RELEASE_HASHES.json`, `release/ea_release.json` and the EX5
are NOT edited by hand (they are repaired by the bot, see "After merge").

## What went wrong
`ea-release` on the #112 merge: `verify_ea_release.py record()` crashed with `UnicodeEncodeError 'charmap'` on the new
"MQ5 version bumped 1.65 → 1.66" print (Windows console is cp1252) → no record written; `--check` then FAILED; but the
pwsh step passed because pwsh only propagates the LAST native command's exit code (`check_ea_fleet_compat.py` → 0); the
commit step pushed **585d2ce9 with only the new unsigned 1.66 EX5** while `#ea` / `ea_release.json` still record 1.65.
`main` is inconsistent (EX5 ≠ record) until the bot re-records.

## Fixes
1. **`ea-release.yml` "Record + sign"**: `$ErrorActionPreference = 'Stop'`, `$PSNativeCommandUseErrorActionPreference = $true`,
   and an explicit `if ($LASTEXITCODE -ne 0) { throw … }` after pip and after each of the three python calls.
2. **Console encoding**: step env `PYTHONUTF8: "1"`, `PYTHONIOENCODING: "utf-8"`; `verify_ea_release.py` is now
   **ASCII-only** (`->`, `...`, `-`) and reconfigures stdout/stderr with `errors="backslashreplace"` so no print can raise.
3. **Commit step** (`set -euo pipefail`): new `scripts/check_ea_commit_consistency.py --staged` runs between `git add` and
   `git commit` — refuses (exit 1, clear message, nothing pushed) when the EX5 is staged without both records, when any
   record's version ≠ the MQ5 `#property version`, when `ex5_sha256` ≠ the EX5 on disk, or when a record is unsigned.
4. **Tests** `tests/unit/test_v1_60_61_hotfix_ea_release_chain.py` (7): source ASCII-only + stdout hardening; `record()`
   forward-bump and refusal paths run with stdout = strict cp1252 (no exception, message intact); `--check` in an empty
   shell with `PYTHONIOENCODING=cp1252` → no traceback; workflow lint (error-preference lines, UTF-8 env, exit-code check
   after every python call, commit gate between add and commit); the gate rejects the 585d2ce9 shape and accepts a
   consistent set; CLI exit codes in a scratch repo.

## After merge — produce the 1.66 record (the hotfix does not touch the MQ5, so the push trigger will NOT fire)
1. Actions → **ea-release** → *Run workflow* → ref `main`. Expected: "Record + sign" prints `OK: MQ5 version bumped 1.65 ->
   1.66 - capturing the new source ...`, `OK: EX5 recorded - sha256 ... - signed`, `--check` OK, fleet gate
   `OK: ... accepts the fleet stable EX5` (signed previous = 1.65); the commit step prints `OK: staged ea-release set is
   consistent (...)` and pushes the bot commit (record 1.66, `previous` 1.65, EX5, rc_lock).
2. On `main` after that commit: `python scripts/verify_ea_release.py --check` → `OK: EX5 ... signature verified`;
   `python scripts/check_release_hash_drift.py` → `OK: MQ5 1.66 matches ...`.
3. **Only then tag `v1.60.61`.** Until step 1 lands, LIVE-attested accounts stay fail-closed (`EA_RELEASE_HASH_UNPINNED`);
   the DEMO fleet is unaffected and keeps running 1.65.

## Security audit #39 (this branch)
PASS / no P0–P2 (`docs/SECURITY_AUDIT_39.md`). P3 applied: `economic_calendar.parse_feed_xml()` — 2 MB cap + DOCTYPE/ENTITY rejection
before stdlib ElementTree (entity-expansion DoS from a hostile feed host), stdlib only, unit test. Other P3s (credential-less wildcard
CORS baseline, EA-reported loosened stop flagged-not-rejected) unchanged / noted.

## CI
`ruff check backend --select E9,F63,F7,F82,F823` ✓ · `check_local_import_shadows.py` ✓ · unit lane ✓ · YAML parses ·
secret_scan 0 · protected-paths ✓ (no protected file in the diff) · `requirements.txt` unchanged.
