# STOIC — Production Verification Runbook

Operational sign-off checklist before funded PAMM capital. Every item below
must be executed BY AN OPERATOR against the REAL production system
(https://www.stoicaibot.com) and/or a REAL demo MT5 account — the code paths
are already machine-verified in CI (3,900+ tests incl. the Risk-Truth HTTP
fail-safe suites), but production proof requires the live environment.

Record each result in the sign-off table at the bottom. Do NOT proceed to
funded PAMM until every row is PASS.

---

## 0. Prerequisites
- Production deploy is current (release tag built by `release.yml` — the
  BUILD_SHA == release-commit gates must be green).
- A demo (non-funded) MT5 account paired via the installer, EA v1.55+
  heartbeating with `installation_id`.
- Admin access recovered (`ADMIN_PASSWORD_FORCE_RESET` flow) and password
  rotated.
- Resend domain verified (resend.com/domains) with `SENDER_EMAIL` on
  stoicaibot.com, otherwise reset emails only reach the Resend account owner.

---

## 1. Auth lifecycle (production)
| Step | Action | Expected |
|---|---|---|
| 1.1 | Register a fresh user (real inbox) | Verification email arrives; account usable |
| 1.2 | Login with wrong password 6× | Rate-limit / lockout response, no user enumeration |
| 1.3 | Login with correct password | Session established; dashboard loads |
| 1.4 | "Forgot password" | Email arrives via Resend; link works ONCE; old password dead |
| 1.5 | Enable MFA (TOTP) on the test user | Step-up required on next sensitive action |
| 1.6 | Logout | Session cookie invalidated — back button shows no authed data |
| 1.7 | Admin login | Forced rotation on first login after force-reset; new password sticks across redeploy |

## 2. Dashboard → Account → MT5 → Host Agent → Broker (E2E)
| Step | Action | Expected |
|---|---|---|
| 2.1 | Add account in UI, run PowerShell installer on the VPS | Pairing claimed; `STOIC-Installation.txt` written |
| 2.2 | EA attached in terminal | Heartbeat within 60s: balance/equity/positions visible in dashboard |
| 2.3 | Check identity | `verified_identity` stamped (account number + broker server match); EA version = latest |
| 2.4 | `GET /api/setup/...` status page | All green: lease, identity, artifact digests match X-STOIC-SHA256 |

## 3. Kill switch / CLOSE_ONLY / emergency flatten (demo only)
| Step | Action | Expected |
|---|---|---|
| 3.1 | Open a small demo position via the bot | Trade visible in both terminal and dashboard |
| 3.2 | `POST /api/operator/action` `{"action":"freeze_trading"}` | Config → observe; NO new orders dispatched; existing position untouched |
| 3.3 | `POST /api/operator/action` `{"action":"panic_mode"}` | EVERY account frozen to observe immediately |
| 3.4 | CLOSE while frozen | Close/reduce is ALLOWED (fail-safe asymmetry) — position flattens in the terminal |
| 3.5 | Order-rate kill switch | >5 orders/min (prod default `MAX_ORDERS_PER_MIN`) → hard block, no bypass |

## 4. Broker connectivity loss → fail-closed
| Step | Action | Expected |
|---|---|---|
| 4.1 | Kill the terminal / disconnect VPS network | Heartbeat goes stale; dashboard shows degraded/stale account |
| 4.2 | Trigger a signal while disconnected | Execution BLOCKED (no lease / identity gate) — decision log shows the veto reason |
| 4.3 | Reconnect | Reconciliation runs; ghost-close recovery cleans any divergence; state converges to broker truth |

## 5. Stale Position Truth / Risk Truth → new trades blocked
| Step | Action | Expected |
|---|---|---|
| 5.1 | Stop EA heartbeats (leave backend running) past freshness window | Risk telemetry → `RISK_UNKNOWN` |
| 5.2 | Attempt BUY / increase exposure | BLOCKED with risk_snapshot recorded (snapshot carries commit + image digest) |
| 5.3 | Attempt CLOSE / reduce | ALLOWED — reducing exposure must never be blocked by unknown risk |
| 5.4 | Resume heartbeats | Telemetry fresh; BUY path re-opens |

## 6. ExecutionIntent UNKNOWN / lost-ACK reconciliation (real MT5)
| Step | Action | Expected |
|---|---|---|
| 6.1 | Dispatch a demo order, kill the EA between dispatch and ACK | Intent parks in UNKNOWN — NOT retried blindly (no duplicate order) |
| 6.2 | Restart EA | Reconciliation resolves intent against broker history: either confirmed (position exists) or void |
| 6.3 | Audit | `pamm_position_truth` and broker terminal agree exactly; decision trace shows the resolution |

## 7. Sniper — Shadow then Demo
| Step | Action | Expected |
|---|---|---|
| 7.1 | Run Sniper in Shadow mode ≥1 week | Paper decisions logged with full veto/explain traces; zero live orders |
| 7.2 | Compare shadow fills vs live market | Slippage evidence sample count ≥ minimum; p95 within envelope |
| 7.3 | Promote to Demo | Real demo orders; guard invariants (spread/slippage/NAV freshness) hold on every fill |

## 8. Chaos campaign (production infra, demo accounts)
Run the built-in drills: `GET /api/ops/chaos` then `POST /api/ops/chaos/run`
(admin + metrics token). Drills cover: duplicate order, broker disconnect,
volatility shock, worker crash, API timeout, clock skew, alert dedup, DB
recovery, config rollback, artifact rollback, panic recovery, backup restore,
invalid signature, command replay.
Expected: every drill reports recovered/fail-closed; no drill produces a live
order on a funded account; alerts fire and dedupe.

## 9. 14-day soak (before funded PAMM)
| Step | Action | Expected |
|---|---|---|
| 9.1 | `POST /api/soak/start` on the demo fleet | Soak session opens |
| 9.2 | Daily `POST /api/soak/checkpoint` | 14 consecutive daily checkpoints, zero Sev-1 incidents |
| 9.3 | Any incident → `POST /api/soak/incident` | Logged, triaged; clock rules per policy (restart on Sev-1) |
| 9.4 | `GET /api/soak/evidence` at day 14 | Hash-chained evidence complete; certification issuable |

## 10. Public testimonials
Status: the landing-page quotes are ILLUSTRATIVE PLACEHOLDERS and are now
labeled as such in three places (section note, per-card badge, footer
disclaimer). The trust-bar stats are live platform data.
Action before removing labels: collect real quotes with WRITTEN authorization,
swap them into `frontend/src/components/LandingTestimonials.jsx`
(TESTIMONIALS array) and delete the labels only then.

---

## 11. Production-proof data collection (runs alongside the soak)
These are MEASUREMENT campaigns, not code work. They require live demo/prod
fills accumulating over days–weeks.

| Item | How | Done when |
|---|---|---|
| 11.1 Segmented execution data | Automatic since iter-153: every measured fill lands in `db.execution_quality` keyed by broker_server × symbol × session. Review via `GET /api/execution/segments?days=30` (admin) | Every traded segment shows `evidence_ready.VERY_HIGH: true` |
| 11.2 Calibrate 10/20 slippage thresholds | Compare per-segment p95/worst distributions from 11.1 against the global `MIN_SLIPPAGE_SAMPLES = {VERY_HIGH: 10, MAXIMUM: 20}`; raise/lower per evidence | Thresholds justified by ≥2 weeks of segment data |
| 11.3 Digital Twin calibration | `GET /api/twin/summary` + `/api/twin/stress` — compare twin-predicted vs realized fills/PnL on demo | Twin error inside agreed tolerance over 100+ trades |
| 11.4 Execution Alpha | The `execution_alpha` gate (strategies registry) logs its decisions — measure realized savings (avoided adverse fills) vs a naive baseline | Positive alpha with confidence interval reported |
| 11.5 Uncertainty calibration | `GET /api/trades/calibration` — predicted win-prob vs realized frequency buckets | Calibration curve within tolerance band |
| 11.6 Factor exposure normalization | BEFORE Multi-Strategy PAMM: move `factor_lots` (signed lots) to normalized risk exposure (%-of-NAV per factor). Code task tracked P1 in the backlog | Design reviewed + implemented + guard tests updated |


---

## Sign-off

| # | Item | Result (PASS/FAIL) | Operator | Date | Evidence link |
|---|---|---|---|---|---|
| 1 | Auth lifecycle | | | | |
| 2 | Dashboard→MT5 E2E | | | | |
| 3 | Kill switch / CLOSE_ONLY / flatten | | | | |
| 4 | Broker-loss fail-closed | | | | |
| 5 | Stale truth blocks new trades | | | | |
| 6 | Lost-ACK reconciliation | | | | |
| 7 | Sniper shadow + demo | | | | |
| 8 | Chaos campaign | | | | |
| 9 | 14-day soak | | | | |
| 10 | Testimonials genuine/labeled | LABELED (illustrative) | agent | 2026-06 | LandingTestimonials.jsx |
