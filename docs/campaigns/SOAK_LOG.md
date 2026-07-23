# STOIC — 2-Week Soak Test Log

**Gate**: docs/MT5_VALIDATION_CAMPAIGN.md §7 — final gate before the live switch.

## How to run
1. Tag the release: push via "Save to GitHub", then create tag `vX.Y.Z` on
   GitHub (Releases → Draft new release). The release workflow only publishes
   after the FULL CI gates + EA compile pass on that exact commit.
2. Deploy the tag on the soak server: `deploy/update.sh vX.Y.Z`
   (auto-verifies the complete topology, auto-rolls back on failure).
3. Kick off: `deploy/soak.sh start`
4. Run each drill at least once per week (`deploy/soak.sh drill alert|recovery|panic`).
5. Check progress anytime: `deploy/soak.sh status`

## Exit criteria (all must hold over the full 14 days)
- [ ] Zero unresolved-accepted states older than 5 min
- [ ] Zero duplicate orders
- [ ] Reconciliation delay p95 < 60 s
- [ ] All drill alerts delivered
- [ ] Audit log complete for every sensitive action

## Weekly drill checklist
| Week | Alert drill | Recovery drill | Panic drill |
|------|-------------|----------------|-------------|
| 1    |             |                |             |
| 2    |             |                |             |

## Log
<!-- deploy/soak.sh appends timestamped entries below -->
