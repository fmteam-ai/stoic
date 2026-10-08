# Tagged release runbook (staged-tree pipeline, round 13)

The release workflow (`.github/workflows/release.yml`) runs on `push` of a tag matching `v*`.
It cannot be triggered from the preview environment — a human with repository write access runs it.

## 0. Prerequisites (GitHub → Settings → Secrets and variables → Actions)
| Secret | Purpose |
|---|---|
| `RELEASE_SIGNER_URL`, `RELEASE_SIGNER_TOKEN`, `RELEASE_SIGNER_ALLOWED_HOSTS`, `RELEASE_SIGNER_KEY_ID` | external (KMS/HSM) Ed25519 signer used to re-sign the model manifest in the staged tree and the RC lock |
| `RELEASE_PUBLIC_KEY_B64` | pinned public key — every `model_manifest verify` in CI and the runtime container verifies against it |
| `RELEASE_EVIDENCE_KEY` | ledger-anchor key for the hermetic verification evidence |
| `GITHUB_TOKEN` | provided automatically (GHCR push, cosign keyless) |

Missing signer secrets fail the run at *"Stage release evidence"* — nothing is published.

## 1. Preflight on the commit you intend to tag
```bash
bash scripts/release_preflight.sh          # test-manifest drift, summary drift, provenance consistency, secret scan, model manifest
```
All five checks must print OK. Commit any regenerated evidence first (`docs/TEST_MANIFEST.md`, `release/rc_lock.json`,
`docs/RELEASE_SUMMARY.md`).

## 2. Tag and push (use the platform's **Save to GitHub**, then tag from a local clone)
```bash
git fetch origin && git checkout <commit>
git tag -a v1.0.0-rc.1 -m "release candidate 1 — staged-tree pipeline"
git push origin v1.0.0-rc.1
```

### 2a. Cutting **v1.60.3** (demo phase, EA 1.62) — exact order
The Release job `Reproducibility` refuses a tag whose tree has no SIGNED `release/ea_release.json` for the shipped MQ5
with the EX5 committed, so the ea-release bot commit MUST be the tagged commit (or an ancestor of it).
1. **Save to GitHub** → merge the PR into `main` (CI push run green; `release_preflight.sh` is 6/6 OK on this tree).
2. Actions → **ea-release** → Run workflow (branch `main`). It must pass *Signer identity preflight*, compile 1.62 with
   0 errors, sign through the Fly signer and push `ea-release: signed EX5 record for <sha> [skip ci]` to `main`
   (`release/ea_release.json`, `docs/RELEASE_HASHES.json`, `release/rc_lock.json`, `backend/static/EmergentTradingBridge.ex5`).
   A `::warning::` about signer security headers is advisory (redeploy the signer when convenient).
3. From a local clone:
   ```bash
   git pull --ff-only origin main
   python scripts/verify_ea_release.py --check          # must print OK (signed 1.62, EX5 hash recorded)
   python scripts/check_release_hash_drift.py           # OK, no WARN
   git tag -a v1.60.3 -m "v1.60.3 — demo phase: EA 1.62 signed, VPS Agent v1.3, main110-113 reviews, audits #11-14"
   git push origin v1.60.3
   ```
   The bot commit carries `[skip ci]`, so the tag push alone does NOT start `Release`.
4. GitHub → Releases → **Draft a new release** → choose tag `v1.60.3` → Publish. The `published` event runs
   `release.yml` (unit+truth+integration from the archive, hermetic `verify_release.sh`, full-topology readiness, MetaEditor
   reproducibility against the signed record, signed attestation, image build/push) and then `deploy-production`.
5. On the server: `deploy/backup.sh && UPDATE_HOLD_ON_FAILURE=1 STOIC_READINESS_POLICY=onboarding-close-only deploy/update.sh v1.60.3`.

## 3. What the run does, in order (each step is a hard gate)
1. `hermetic-verify` — exports the exact tag tree, writes `BUILD_SHA`, re-signs the model manifest for that commit, runs
   `model_manifest verify --build <commit>` and the full `verify_release.sh` (declared test lanes, frontend build/E2E,
   provenance consistency) inside a clean checkout with no `.git`.
2. `release` — stages ONE tree (`/tmp/pkg`): re-signs the manifest there, builds backend/frontend images **from the staged
   tree**, extracts `BUILD_SHA` / `MODEL_MANIFEST.json` / model binaries from the candidate image and compares them to
   the tree **before push**, pushes by digest, binds `rc_lock.json` (authoritative=true, image digests), regenerates
   `RELEASE_SUMMARY.md`, runs `release_consistency_check.py --strict`, packs the archive, signs images (cosign keyless)
   and publishes SBOMs + attestations.

## 4. Artifacts to keep as P1-04 evidence
`stoic-<tag>.tar.gz`, `rc_lock.json` (authoritative), `SHA256SUMS(.sig)`, SBOMs, cosign signatures, the hermetic
verification evidence and the run URL. The deployed `/api/version` must report the tag's commit; then the operational
acceptance run (`scripts/staging_acceptance.sh` on the promoted image) can start collecting inventory, broker-truth and
EA-consistency evidence. Authority stays BLOCKED/CLOSE_ONLY until that run is signed.

## 5. If the run fails
- *Stage release evidence*: signer secrets missing/invalid or the developer-signed manifest no longer verifies (re-sign
  locally with two admin approvals: `python -m model_manifest sign --promote <uid> --approval email:event:principal …`).
- *Extract provenance… BEFORE push*: image content differs from the staged tree — the Dockerfile copied something that is
  not in the tree; never bypass, fix the Dockerfile.
- *Bind release evidence… consistency gate*: a field-level report names the disagreeing identifier.
