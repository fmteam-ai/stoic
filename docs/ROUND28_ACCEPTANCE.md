# Round 28 — Audit remediation & operator runbook

## Code remediation — DONE (tests: `tests/integration/test_r28_audit.py`, `tests/unit/test_r28_chart_provenance.py`, `tests/test_r28_http.py`)

| Finding | Fix |
|---|---|
| **P1-01** DEMO approval not bound to identity | `broker_env.attestation_identity()` — SHA-256 over immutable account id, normalized broker, declared + EA-reported server, account number, account type, verified installation id, terminal login and `creds_version`. Stored as `environment_attestation.identity_hash`; `attested_environment()` recomputes on every read, so any change (incl. credential rotation → `$inc creds_version`) voids the approval (`attestation_state=invalidated`, gate code `EA_DEMO_ATTESTATION_INVALIDATED`). Re-attestation = fresh admin re-auth. Legacy attestations without a digest are not honoured. |
| **P2-01** pricing process-local | `platform_state.plan_pricing.pricing_version` (`$inc` on every save). `plan_settings.ensure_fresh(db)` runs before plans listing, checkout, registration trial grant; `pricing_version` is in every public plan, checkout metadata and the audit event. `payment_transactions` carry `amount_minor/currency/pricing_version`; `apply_successful_payment` refuses fulfilment (`skipped_reason=price_mismatch`) when Stripe's settled amount/currency does not reproduce the snapshot. |
| **P2-02** vault key coupled to JWT | `APP_ENV=production` boots only with a dedicated `SECRETS_MASTER_KEY` (32 bytes base64). Undecryptable sealed values are recorded (`source=vault_undecryptable`) and surface as `checks.secrets_vault` in `/api/ops/release-readiness`. `master_key_id` removed from the status API. |
| **P2-03** trial eligibility lazy | `users.trial_grant` written atomically at registration (tier, days, start/end, `offer_version`, granted_at). `get_subscription` honours the stored grant; later offer edits/disable never change it. Unique `subscriptions.user_id` race handled. |
| **P2-04** USD-named fields | `amount_minor`, `effective_monthly_minor`, `savings_minor` + `currency` on every plan; `*_usd` kept as deprecated compatibility fields. |
| **P2-05** chart provenance | `chart_provenance.build()` contract (provider, source_kind ∈ broker_reconciled/indicative/simulated/derived, as_of UTC, freshness, stale, missing intervals (weekend-aware for daily), cache/fallback status, environment, ledger/reconciliation id). Attached to market history, accounts equity-curve, verified performance, PAMM NAV, research walk-forward; rendered by `components/ChartProvenance.jsx` with visually distinct kinds. |

## Operator / release-process items — RUNBOOK (not closable from code)

### P1-02 · authoritative release provenance
1. Freeze all files, then tag: `git tag -s vX.Y.Z && git push --tags`.
2. CI `release.yml` on the **peeled signed tag** must: run gates → build backend/frontend images → record digests → generate SBOMs (`syft`) → sign admission record (signer sidecar) → run `scripts/freeze_rc_lock.py --commit $(git rev-parse <tag>^{}) --backend-digest <sha256> --frontend-digest <sha256> --target production --evidence sbom_backend=<path> --evidence sbom_frontend=<path> --evidence admission=<path>` → commit `release/rc_lock.json` + `release/attestation.<sha>.json`.
3. `deploy/update.sh` with `ATTESTATION_REQUIRED=true` then pulls the attested GHCR digests; `release_consistency_check.py --strict` must exit 0 (no developer-snapshot notes).

### P1-03 · EA executable chain
1. On the sanctioned Windows build host: `MetaEditor64.exe /compile:backend\static\EmergentTradingBridge.mq5 /log` — 0 errors, record MetaEditor build number.
2. `python scripts/verify_ea_release.py --ex5 <path>.ex5 --compile-log <log> --metaeditor-version <ver> --windows-build <build> --mt5-build <build>` (records hash + compiler identity), then re-run with `--sign` (signer sidecar).
3. Set `EA_RELEASE_SHA256` (or ship `release/ea_release.json`) in the release; `release-readiness.ea_release.ok` becomes true. A locally compiled EX5 may trade **only** on an admin-attested, identity-bound DEMO account (see P1-01).

### P1-04 · live trading truth
Run the signed read-only acceptance pack against the promoted digests: `deploy/doctor.sh --bundle`, `GET /api/ops/release-readiness`, AT-01 drill; attach broker statements. Required evidence: 6 unique accounts, exactly 3 approved LIVE accounts ON with 3 matching bots ON, identical EA expected/connected/stale counts on every surface, fresh reconciliation with matching broker positions, zero UNKNOWN/aged execution intents, Safety Blocks + Bot Health caps dominating, performance matching statements after fees/swaps.

### P2-06 · execution evidence
Attach to the release package: CI run URL + signed results, JUnit XML (`test_reports/pytest/*.xml`), coverage, service versions, `docs/TEST_MANIFEST.md` hash (= `rc_lock.test_manifest_sha256`), candidate image digests.
