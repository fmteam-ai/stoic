# Phase 1 — Remove the traps (STOIC Simple Onboarding Spec) · EA 1.64

One release. Items U2 U3 U8 U9 U10 U11 U16 U18 O1 O2 O3. Guardrails unchanged: signed EA, verified terminal,
broker truth wins, real money needs a human, signed releases, full audit, fail closed.

| ID | Change | Done when → how to verify |
|----|--------|----------------------------|
| U2 | `server_adopt.py`: token-authenticated heartbeat with the SAME MT5 login and a different/empty server name → server adopted, chain reset, audit `server_adopted_from_ea` (login, installation id, from/to). Login mismatch never adopts. Runs before the identity chain so the same heartbeat verifies. | Register "OnEquity-Demo", EA on OnEquity-Server → card shows OnEquity-Server after the first heartbeat; audit chain has `server_adopted_from_ea`. |
| U3 | Edit dialog on every card (label, group). Server field admin-only (`PATCH server` → admin role + step-up, audited); users see "adopted automatically from the EA". | Label typo fixed in 2 clicks. |
| U8 | EA 1.64 `IsLegacyDefaultUrl()`: www host, `your-app.preview…`, any `*.preview.emergentagent.com` count as default → STOIC-Server.txt / `https://stoicaibot.com`. | Re-attach over an old EA (inputs kept) → Experts tab: "is an old compiled default — treating it as default", polls stoicaibot.com. |
| U9 | `auto_attest.py`: DEMO attested automatically when identity authoritative + broker reports `demo` + installer-measured EX5 hash ∈ v2-signed set (N99-2: heartbeat echo is never evidence) + declared DEMO + `demo_proof` ok. `approved_by: system:auto-demo`, verifier `ea_heartbeat_auto`, chained audit. Voided + audited on real/contest. Manual attestation never overwritten. | New demo account shows DEMO verified without an admin step; Admin → Account Environments shows the auto proof. |
| U10 | `position_mode_resolution`: EA-reported ACCOUNT_MARGIN_MODE first (source `ea`), admin setting only without an EA report and flagged when contradicted. | Mode column source = EA on every heartbeating account. |
| U11 | `bot_inventory.py`: exactly one bot_config bound at account creation (inactive until Start); no account-less profile (seed/signup stopped; default scope resolves to the user's account, virtual defaults when none); startup migration deletes null/unknown-account configs (audited `bot_configs_orphans_deleted`); delete removes the account's configs. | Inventory never shows an orphan. |
| U16 | Install Progress: EA self-check `webrequest_ok:false` → "MT5 blocks the address" + guided fix; "no request reached the server" wording; 401 → REJECTED (M122). | Each 2026-10-11 failure shows its real cause. |
| U18 | `account_tombstones.py`: delete keeps token hashes 90 days; bridge answers **410 account_deleted**; EA 1.64 prints the reason, `ExpertRemove()`; one `low` finding `deleted_account_poll`; B2 ignores tombstoned polls. | Delete while the EA runs → Experts tab: "REMOVED on the STOIC dashboard (HTTP 410) — EA stopping"; no CRITICAL. |
| O1 | Security agent: Cloudflare edge IP as target → no proposal, finding `proxy_misconfiguration` (high) with the TRUST_CF_CONNECTING_IP fix, never `protected_target` CRITICAL; valid-heartbeat IP exemption window 10 min. | No false CRITICAL during onboarding. |
| O2 | `install.sh --cloudflare` sets `TRUST_CF_CONNECTING_IP=true`; `doctor.sh` FAILS when the overlay is active without it; compose overlay exports `CLOUDFLARE_MODE=true` so `demo_readiness` client_ip check fails closed. | doctor.sh red line when misconfigured. |
| O3 | `update.sh <tag>`: `scripts/release_precheck.py` BEFORE the backup — tag on origin + Release workflow concluded success (GitHub API, `GITHUB_TOKEN` from .env); refuses otherwise with "nothing was changed on this host"; `--allow-unattested` overrides; API unreachable → continue to the attestation gate. | Deploying an unfinished tag changes nothing on the host. |

Tests: `backend/tests/unit/test_phase1_onboarding.py` (14) + updated SA3/SA4/inventory/registry tests; unit lane 1298 pass;
live smoke on preview (create → bound bot → heartbeat adopts server, mode=netting/ea → delete → 410 → one low finding).
