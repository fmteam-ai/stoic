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

### P1-03 · EA executable chain — NOW AUTOMATED (`.github/workflows/ea-release.yml`)
The sanctioned job is the **only** producer of a trusted EX5 record:
1. **Trigger**: GitHub → Actions → *ea-release* → *Run workflow* (branch `main`), or automatically on any push touching `backend/static/EmergentTradingBridge.mq5`.
2. The job (windows-latest) installs MetaTrader 5, compiles the exact MQ5 with `metaeditor64.exe` (0 errors enforced), reads the MetaEditor build **from the compile log**, hashes the EX5, signs the record via the **external signer** (`RELEASE_SIGNER_URL/TOKEN/ALLOWED_HOSTS`, `RELEASE_PUBLIC_KEY_B64`, `RELEASE_SIGNER_KEY_ID` repo secrets — the private key never enters CI), binds it to `GITHUB_SHA` (`source_commit`, `compiled_by=github-actions`) and **commits** `release/ea_release.json` + `docs/RELEASE_HASHES.json` + the rc_lock toolchain mirror back to the branch.
3. `release.yml` re-compiles on the tag and **blocks the release** unless the freshly compiled EX5 hash equals the committed signed record (reproducibility) and `verify_ea_release.py --check` passes; it exports `EA_RELEASE_SHA256` for the image pin.
4. Runtime trust (`ea_capabilities.expected_ea_sha256`): only `EA_RELEASE_SHA256` env or a record that is Ed25519-signed, `compiled_by=github-actions`, version == LIVE_MIN, and whose `mq5_sha256` equals the MQ5 the server ships. Unsigned/manual/drifted ⇒ `None` ⇒ live accounts stay blocked. Docker image ships `release/ea_release.json`.
5. After the bot commit lands: `./deploy/update.sh` → `release-readiness.ea_release.ok=true` → live accounts pass `live_gate` once their terminal reports the matching `ea_binary_sha256` on heartbeat.
Prerequisite: the external signer must be provisioned (repo secrets above). Manual `verify_ea_release.py` runs now produce `compiled_by=manual` records that the gate **refuses** by design.

### P1-04 · live trading truth
Run the signed read-only acceptance pack against the promoted digests: `deploy/doctor.sh --bundle`, `GET /api/ops/release-readiness`, AT-01 drill; attach broker statements. Required evidence: 6 unique accounts, exactly 3 approved LIVE accounts ON with 3 matching bots ON, identical EA expected/connected/stale counts on every surface, fresh reconciliation with matching broker positions, zero UNKNOWN/aged execution intents, Safety Blocks + Bot Health caps dominating, performance matching statements after fees/swaps.

### P2-06 · execution evidence
Attach to the release package: CI run URL + signed results, JUnit XML (`test_reports/pytest/*.xml`), coverage, service versions, `docs/TEST_MANIFEST.md` hash (= `rc_lock.test_manifest_sha256`), candidate image digests.

---

# Round 29 — remediation (tests: `tests/integration/test_r29_audit.py`)

| Finding | Fix |
|---|---|
| **P1-01** checkout mixes pricing versions | `subscription_plans.pricing_snapshot(plan)` taken right after `ensure_fresh()`; Stripe request, metadata, ledger row (`payment_transactions.snapshot`), response and fulfilment read only the snapshot. Per-checkout `idempotency_key`. Concurrency test: admin update during the Stripe await → ledger/metadata/response all old version, fulfilment accepts the settled old amount. |
| **P1-02** DEMO truth | `broker_env.demo_proof()` — attestation requires: authoritative terminal identity, EA-reported server == declared, EA-reported login == account number, heartbeat ≤10 min, no live-capital indicator. `server_demo_named` may be replaced only by an explicit admin **override** (`verifier=admin_override`, reason ≥10 chars, red in the panel, in the audit chain). Proof id/checks/verifier stored on the attestation; attestations without a verifier are not honoured. |
| **P2-01** verified-performance freshness | `as_of` = oldest of (last broker deal, freshest reconciliation watermark, freshest heartbeat); `reconciliation_id = recon:<acct>:<seq>,…`; missing watermark ⇒ `stale`, `share_allowed=false`, `POST /performance/share` refused, UI button locked. |
| **P2-02** trial decision | `users.trial_decision` = granted / not_eligible / pending_error (+offer_version). `pending_error` retried idempotently before the first entitlement against the ORIGINAL offer version; if the offer moved, it waits for an explicit migration. Explicit decisions never fall back to lazy evaluation. |
| **P2-03** fulfilment uses current globals | Proration tier/duration/base prices come from `payment_transactions.snapshot`; `subscriptions.fulfilled_pricing_version` recorded. |
| **P2-04** vault rotation | `key_version` on sealed docs; dual-read via `SECRETS_MASTER_KEY_PREVIOUS`, single-write with current key; `POST /api/admin/integrations/rewrap` (re-auth) writes a `secrets_rewrap_manifests` record, re-seals all, verifies completeness; workers stamp `vault_key_id` on their lease; `release-readiness.secrets_rewrap` blocks until manifest complete and all workers on the new key; production blocks while `_PREVIOUS` is still set. **Rotation procedure**: set `SECRETS_MASTER_KEY=<new>`, `SECRETS_MASTER_KEY_PREVIOUS=<old>`, bump `SECRETS_MASTER_KEY_VERSION`, `deploy/restart.sh`, run rewrap in Admin → Integrations, confirm readiness, remove `_PREVIOUS`, restart. Rollback: swap the two keys back before removing `_PREVIOUS`. |
| **P2-05** provenance enforcement | Pydantic `ChartProvenance` model validates every contract; `FINANCIAL_SERIES_ENDPOINTS` inventory + CI test asserts each endpoint attaches it and every `<ResponsiveContainer>` chart renders `<ChartProvenance>`; UI renders a red **PROVENANCE MISSING** block for absent/invalid contracts. |

Operator items **P1-03 / P1-04 / P1-05 / P2-06** unchanged — follow the Round-28 runbook above with the v100 source.
