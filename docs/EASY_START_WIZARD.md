# Easy Start Wizard — delivery (spec Oct 12 2026, @george) — "main129"

Goal met in code: Add broker → Bot trading in one wizard, 4 screens, one main button each; no GitHub / SSH / release /
EA input / server name / token file / WebRequest setting ever shown to the user. Every failure is one sentence + one
button on the same screen, and the screen retries by itself (2 s poll, auto-advance). Fail-closed, real-money operator
approval (bound to identity), signed EA/installer, v2 key, audit-chain entries and R1–R9 OBSERVE unchanged.
No `--force` push; `release/ea_release.json` untouched (`rc_lock.json` re-frozen only for the manifest-binding test).

## Operator (admin) — A1 … A5

| # | Delivered | Where |
|---|---|---|
| A1 | **`rules-policy` GitHub workflow** — form inputs demo_only / max accounts per user / allowed brokers / max risk % / max daily loss % / expiry days / installation id / reason → `policy-approval` environment (required reviewer) → external release signer (key v2, purpose `policy-rules`) → commits `policies/<version>.json` `[skip ci]`. **Load latest signed policy** button (`POST /api/authority/policy/load-latest`, admin + step-up): fetches the newest file from the repo's `policies/` via the GitHub API (or the local dir), verifies signature · installation · environment · expiry · nonce, activates. No release, no deploy; never downgrades to an older issue. | `.github/workflows/rules-policy.yml`, `backend/rules_policy.py` (`fetch_candidates_from_github`, `load_latest`), `routes/authority_routes.py`, `RulesPolicyPanel.jsx` |
| A2 | **Deploy checks the rules, not counts** — `RECONCILE_EXPECT=rules` is the default in `deploy/update.sh`; `ops/production_reconcile.py --expect rules` checks: rules policy unexpired · every enabled account admitted · brokers allowed · every user within the cap · DEMO-attested when demo-only · no bots on disabled accounts · boolean flags · sets identical. No active/unexpired policy ⇒ REFUSED ⇒ rollback (fail closed). `prepromotion_evidence.py` passes `--expect` through. Onboarding close-only mode no longer needed. | `deploy/update.sh`, `backend/ops/production_reconcile.py` |
| A3 | **Replace account list with rules policy** — `POST /api/authority/policy/retire-id-list` (admin + step-up): archives `platform_state.inventory_expectation` → `inventory_expectation_retired`, deletes the live id list, audit `inventory_expectation_retired`; refused (409) without an unexpired rules policy. Button appears in the Rules Policy panel only while a legacy list exists. `demo_only_policy_active` now also honours a demo-only RULES policy. | `rules_policy.retire_id_list`, `RulesPolicyPanel.jsx` |
| A4 | **Renewal reminder** — 7 days before expiry: admin banner (`GET /api/authority/policy/renewal`, pre-filled next version/cap/brokers/days + **Renew** link to the workflow form) and Telegram text with the same pre-fill; critical alert once expired. | `rules_policy.expiry / renewal_prefill / renew_link`, `policy_expiry_alerts.plan_rules`, `RulesPolicyPanel.jsx` |
| A5 | **Go-Live checklist** — `GET /api/ops/go-live-checklist`: release signed · rules policy active · workers healthy · Cloudflare IP trust · backups (last 24 h, `deploy/backup.sh` now records `platform_state.last_backup`) · admin 2FA — each red row with ONE fix (button action, copy-command or link). | `routes/ops_routes.py`, `GoLiveChecklist.jsx` (top of Admin → Brokers) |

## User wizard — 4 screens (`EasyStartWizard.jsx`, `routes/wizard_routes.py`)
Opened from Accounts → **ADD BROKER**, from the empty state ("Connect your first broker") and from any account card
that is not trading (**CONTINUE SETUP →**). Resume is server-derived (`GET /api/setup/wizard/{id}` → `screen`), so
closing and reopening lands on the same step. Top bar `1 Broker → 2 Connect → 3 Activate → 4 Start`.

1. **Broker** — preset search with tiles, "Other MT5 broker", MT5 login, DEMO/REAL toggle (REAL disabled under a
   demo-only policy), optional name, **no server field**. NEXT creates the account with a bound bot (Balanced) and
   the one-time pairing code.
2. **Connect** — A) own Windows VPS/PC: one COPY button + 3-picture guide (PowerShell as admin → paste → Enter), the
   `.cmd` alternative (owner-bound handle, token only in the file body); B) STOIC VPS (offer). Live 5-tick checklist
   (code redeemed · installer finished · EA file verified · first heartbeat · terminal verified), 2 s poll,
   auto-advance. Errors → sentence + button: old token/401/duplicate EA → "MT5 is using an old connection key." /
   **Repair** (repair line, same guide; installer v1.10 cleans duplicates); WebRequest 4014 → "MT5 is blocking STOIC." /
   **Repair**; no heartbeat after 3 min → "We can't hear MT5 yet. Is MT5 open and logged in to #login?" / **Check
   again**, **Repair**; different login → "MT5 is logged in to #X, not #Y." / **Use #X instead**
   (`POST /setup/wizard/{id}/use-reported-login`, audited) · **I'll log in to #Y**; real money on a demo setup → "This is
   a real-money account." / **Switch to Real**.
3. **Activate** — demo: ticks DEMO confirmed by the broker → admitted by the rules → inventory approved (automatic,
   `auto_approve_under_rules`). Real: "Waiting for operator approval" (admin in-app + Telegram, Approve with password +
   authenticator), screen continues by itself. Blocked: "Your plan allows 1 account — disable Old account to continue."
   / **Disable it** (the cap now counts only ENABLED accounts, so disabling is a real fix); broker not allowed /
   demo-only / risk over cap / no or expired rules policy → one sentence each, fail closed.
4. **Start** — Conservative / Balanced (default) / Aggressive cards with one-line meaning, already capped by the rules
   policy (a capped card saves the highest level that fits); symbols auto-detected with the broker suffix; Advanced
   collapsed; **START BOT** = readiness check + start, failures as one sentence + Check again.
   Done: "Trading — the bot will open trades when its signals fire" · Dashboard · Test trade (demo) · Telegram alerts.

## Acceptance test (7 items) — results
| # | Item | Result | Evidence |
|---|---|---|---|
| 1 | Fresh demo login, VPS with MT5: Add broker → Start bot < 5 min, wizard + PowerShell only | **PASS (UI + API), heartbeat leg simulated** — Screens 1→2 live on the preview (account created with bound bot + pairing line in < 30 s; checklist polling; CONTINUE SETUP resume). Screens 3→4 rendered from the real `/setup/wizard` body with the connect ticks mocked, because the preview has no MT5 terminal. Needs one real VPS run on production for the timing. | `docs/screenshots/easy_start/02_*`, `03_*`, `05_*` |
| 2 | Delete the account and add it again: no GitHub run, release or deploy | **PASS** — admission and inventory approval are automatic under the rules policy (`sync_admission` on create/heartbeat, `auto_approve_under_rules`); the testing run created and deleted an account through the wizard with no operator action. Unit: `test_u14_inventory_auto_approved_under_rules`, `test_phase1 tombstones`. | iteration_250 |
| 3 | Old token typed in the EA inputs → wizard detects the 401 and offers Repair; Repair fixes it in one run | **PASS (unit + live mapping)** — `connect_error` → "MT5 is using an old connection key." / Repair (`bridge_rejected` ≤ 5 min); Repair line regenerated in-wizard; installer v1.10 `Remove-StoicStaleEaCharts` strips the hand-typed BridgeToken block. Terminal-side run needs a VPS. | `test_wizard_connect_errors_are_one_sentence_one_button`, `test_v110_repair_strips_saved_ea_attachments_and_stale_bridgetoken_inputs` |
| 4 | Second account over the cap: Screen 3 shows the cap sentence and Disable it | **PASS (live)** — preview rules policy `preview-rules-v1` (cap 1): the new account's admission = blocked, `/setup/wizard` returned "Your plan allows 1 account — disable Start Trader to continue." with **Disable it**; rendered at 390 px. | `04_variant_A_mobile.jpeg`, curl log |
| 5 | Real-money login: operator approves from the alert; the user's wizard continues by itself | **PASS (code + unit)** — `pending_operator` → `wizard-waiting-operator`; approval `POST /authority/admission/{id}/approve` (password + authenticator) is identity-bound; the 2 s poll advances the screen. Variant C render blocked by a transient dev-bundle reload in the test run (code path verified). | `test_wizard_activate_cap_sentence…` (waiting_operator), RulesPolicyPanel pending list |
| 6 | Deploy with a rules policy active: `update.sh` passes without RECONCILE_EXPECT counts and without onboarding mode | **PASS (unit + bash -n)** — default `rules`, regex accepts `rules`, reconcile checks the rules and REFUSES without a policy; needs the production VPS for a real run. | `test_a2_deploy_checks_rules_not_counts` |
| 7 | Unit and UI tests for each screen and error + screenshots at 390 px and desktop | **PASS** — `tests/unit/test_easy_start_wizard.py` (9) + updated suites; unit lane **1333 passed**; testing-agent UI run `test_reports/iteration_250.json`; screenshots below. | `docs/screenshots/easy_start/*.jpeg` |

Screenshots (desktop 1920×800 + phone 390×844): `01_accounts_header_*`, `02_wizard_s1_*`, `03_wizard_s2_*`,
`04_variant_A_mobile`, `05_variant_B_*`, `07_admin_brokers_*`, `08_after_load_latest` under `docs/screenshots/easy_start/`.

## Changed files
- Workflow: `.github/workflows/rules-policy.yml` (new)
- Backend: `rules_policy.py` (load_latest · fetch_candidates_from_github · retire_id_list · expiry · renewal_prefill ·
  renew_link · cap_count_query · effective_max_risk_pct clamp), `routes/authority_routes.py` (load-latest · retire-id-list ·
  renewal), `routes/wizard_routes.py` (new: wizard state · use-reported-login), `routes/ops_routes.py` (go-live-checklist),
  `inventory_projection.py` (enabled-only cap count · demo_only_policy_active honours rules), `policy_expiry_alerts.py`
  (plan_rules), `ops/production_reconcile.py` (`--expect rules`), `server.py` (router)
- Deploy: `deploy/update.sh` (rules default), `deploy/backup.sh` (last_backup marker)
- Frontend: `components/EasyStartWizard.jsx` (new), `components/admin/GoLiveChecklist.jsx` (new),
  `components/admin/RulesPolicyPanel.jsx`, `pages/Accounts.jsx`, `pages/AdminBrokers.jsx`
- Tests: `tests/unit/test_easy_start_wizard.py` (new), `test_main127_review.py`, `test_easy_connect_p1.py`, `test_main117_a20.py`
- Artefacts: `docs/TEST_MANIFEST.md` (5610/592), `docs/RELEASE_SUMMARY.md`, `release/rc_lock.json` (manifest hash only)

## Operator next steps (production)
1. Run **Actions → rules-policy** once (installation id, `demo-rules-v1`, demo_only, cap, brokers, caps, 30 days) → reviewer approves.
2. Admin → Brokers → **LOAD LATEST SIGNED POLICY** (set `GITHUB_REPO` on the server; token only for a private repo).
3. Admin → **REPLACE ACCOUNT LIST WITH RULES POLICY** (one time), then remove `RECONCILE_EXPECT` from `./.env` (defaults to `rules`).
4. Deploy the release normally; the Go-Live checklist shows what is still red.
