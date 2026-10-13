# Security Audit #26 — v1.60.29 Decision Cache, commit-message leak remediation, Scalp orphan removal

Scope: every surface changed since audit #25 (`docs/SECURITY_AUDIT_25.md`): `canonical_decision.py` shared
`_cached_decision()` now backing `decide_account` / `decide_platform`; the 2026-10-13 commit-message leak remediation
(`scripts/commit_message_secret_scan.py`, `.gitleaks-commit-messages.toml`, CI `security-scan` job, `scripts/git-hooks/commit-msg`,
`scripts/retired_credentials.sha256`); `routes/scalp_routes.py remove_config` orphan branch + `status.account_missing`;
`account_deletion.purge_account` scalp cascade; v1.60.25–28 re-check; standard perimeter sweep. Read-only: source traced
end-to-end plus unauthenticated probes of the preview; no mutating request, no credential printed or committed.

**Verdict: PASS — no P0/P1/P2. Two P3 defence-in-depth notes; one fixed in this release, one accepted.**

| ID | Sev | Location | Finding | Status |
|---|---|---|---|---|
| SEC-026-1 | P3 | `.gitleaks-commit-messages.toml` | Allowlist for the `Emergent-Commit-Key` trailer was LINE-scoped, so a secret on the same line as a forged trailer would suppress the gitleaks hit (the repo scanner still flagged it). | FIXED — `regexTarget = "match"` anchored to the trailer / bare 64-hex digest only; verified: trailer-only line passes, trailer + token on one line is reported. |
| SEC-026-2 | P3 | `.github/workflows/ci.yml` commit-message range | Fallback `HEAD~20..HEAD` (workflow_call / first push) can skip older commits of a very large first push. | ACCEPTED — backstopped by the blocking full-history `commit_message_secret_scan.py --all` step in the same job. |

## Confirmed safe
- Decision cache: execution choke points bypass it (`trading_authority.enforce_new_trade` / `gate_or_block` call
  `compute_authority` directly; `nl_execution.recheck_authority` uses `fresh=True`). Keys are namespaced (`<user_id>`,
  `acct:<id>`, `platform`) with per-scope fingerprints + the global authority version; PANIC / platform switches call
  `bump_authority_version` (deletes every snapshot) and any account / bot / global-count change flips the fingerprint;
  hits are served only on exact version+fingerprint match under a 5 s TTL and returned as deep copies. No cross-tenant
  collision, no attacker-writable poisoning path.
- Scalp orphan removal: no IDOR — the config write path (`set_config` → `engine.apply_config`) requires account
  ownership and stamps `user_id` = owner, so forged cross-tenant rows cannot exist; the orphan `delete_many` is scoped to
  the caller; another user's live account yields a generic 404; `account_missing` is a bare boolean on caller-owned
  runners only. `purge_account` cascades scalp configs + runners by account id.
- Commit-message controls: gitleaks tarball sha256-pinned; stdin scan of the push/PR range + repo scanner on the range
  and on the full history; remediation itself wrote only sha256 fingerprints (no values) — `secret_scan.py`,
  `secret_scan.py --history` and `commit_message_secret_scan.py --all` all 0 blocking. Known limit: a token split
  across lines is out of scope for both scanners.
- v1.60.25–28 intact: `request-sync` throttle (6/h) + `already_pending`; rules-policy attestation fails closed.
- Perimeter: access/refresh cookies HttpOnly + Secure + SameSite; CSRF double-submit enforced on cookie-authenticated
  mutations incl. `DELETE /api/scalp/config`; credentialed CORS only for the production allowlist; HSTS / CSP / nosniff /
  X-Frame DENY present; generic `401 Not authenticated` bodies.

## Coverage and limits
- No authenticated mutating session was exercised (source + public read-only probes), consistent with prior baselines.
- Accepted from #25: public `/status` 12-char inventory digest + authority version (non-sensitive).
