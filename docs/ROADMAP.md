# STOIC Roadmap

Cleaned into four buckets (iter-212). An item lives in exactly ONE
bucket; "production blocker" means live real-money scale-up waits on it.

## 1 · Production blockers
- [ ] **Signed Host Agent MSI** — pipeline is live
      (`.github/workflows/msi-release.yml`); BLOCKED on the code-signing
      certificate (`CODESIGN_PFX_BASE64` / `CODESIGN_PFX_PASSWORD`
      repository secrets). Independent verification job already enforced.
- [ ] **Broker-attached validation** — checklist automated at
      `GET /api/ops/broker-validation?account_id=`; needs a REAL broker
      account attached (not paper) to turn green.
- [ ] **14-day soak campaign** — machinery live
      (`/api/ops/soak/*`, criteria in `backend/soak_campaign.py`);
      needs 14 elapsed days with daily checkpoints once the real broker
      account is attached.
- [ ] **PAMM broker certification micro-pilot** — first real allocation
      round-trip on the certified broker account.

## 2 · Evidence campaigns (prove what's built)
- [ ] Chaos drill battery in anger: `pytest -m chaos` against staging,
      results archived per release.
- [ ] Load drill: 5000+ concurrent bot loops, tenant-isolation assertions.
- [ ] Conformal coverage review after 30 live days
      (`/api/brain/coverage` segments must hold per strategy/regime).
- [ ] AI Value Ledger review after 30 live days
      (`/api/brain/value-ledger` — observed net_usd must justify gates).

## 3 · Research (no ship date)
- Formal Bailey PBO (multi-config IS/OOS ranking) to replace the
  CPCV OOS-loss-rate proxy.
- Counterfactual estimation for SKIPped decisions (virtual fills) —
  would upgrade "unobservable" ledger entries to "estimated".
- Cross-tenant meta-learning (privacy-preserving strategy priors).

## 4 · Post-GA
- Command Center dashboard (GREEN/YELLOW/RED single screen —
  `/api/brain/health` scopes are the ready data source).
- Investor monthly statements (PAMM PDF exports).
- Trade Intelligence Report frontend page.
- Dependabot auto-bumps with CI-gated upgrade PRs.
