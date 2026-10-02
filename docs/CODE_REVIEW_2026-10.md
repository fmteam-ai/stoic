# STOIC — Full Code Review (2026-10)

Scope: the whole repository (≈100 k lines of backend Python, the MT5 EA, the
crypto bridge, the React frontend, deploy/CI). Five parallel review passes —
order execution & risk, strategy/ML/backtest, security, the LLM layer, and
EA/scalp/workers/hygiene — produced ~70 findings. Every finding listed as
*fixed* below was confirmed by reading the code, fixed minimally in the
surrounding style, and covered by a new or updated test.

**Verification**: `tests/unit` → 808 passed, 1 skipped (baseline 739 + 3
environment-only failures); risk/sizing/engine legacy suites identical to
HEAD; no new lint findings in touched files. MongoDB-backed integration
tests could not run in the review sandbox (no database) and must be run in CI.

---

## 1. Critical & high — fixed

| # | Area | Problem | Fix |
|---|------|---------|-----|
| C1 | ML calibration | Platt fit started at A=+1, i.e. the map p→1−p, and gradient descent never escaped it: **calibrated p_win was anti-correlated (−1.0) with the model**, so the learned-meta gate favoured the worst setups. The reject threshold was also fitted in raw-score units but compared against calibrated scores. | Newton fit from the identity (converges in ~3 iterations); calibrator shipped only if it improves Brier on the eval set; threshold fitted in calibrated units (`probability_calibrator.py`, `learned_meta.py`) |
| C2 | Risk | Risk-engine (drawdown ladder, CVaR, abnormal-market halt) and std-contract clamp **failed open** on any exception; a `return` inside the per-symbol loop silently dropped every remaining symbol | Fail closed (skip + pulse), `continue` (`bot_runner.py`) |
| C3 | Reconciliation | Heartbeat with ticket-count mismatch set `tickets=[]` and **marked live positions closed** (no longer managed, slots freed, bot opens more) | Skip reconciliation on mismatch; status guard on reconcile writes (`bridge_routes.py`, `trade_reconciler.py`) |
| C4 | Execution | Pending market orders had **no age limit** — an EA reconnect after an outage executed hours-old signals at market | Never-dispatched opens older than `MAX_PENDING_OPEN_AGE_S` (120 s) are cancelled `stale_pending`; dispatched-but-unconfirmed ones are excluded from re-dispatch |
| C5 | Crypto | Kraken/Binance.US accounts flagged *testnet* (no sandbox exists) sent **real orders**; bot path skipped the live-enabled gate; retries could double positions | Block both cases; deterministic `clientOrderId` per signal (`crypto_bridge/`) |
| H1 | Risk | Anti-pyramid, SL-cooldown, loss-streak and daily-cap queries matched `symbol` but trades store the broker symbol (`XAUUSD-ECN`) → **never matched** on renaming brokers; daily cap filtered a field (`created_at`) trades don't have | Match `symbol` OR `base_symbol`; cap on `opened_at` |
| H2 | Risk | Safety Guardian daily-loss cap saw $0 for default-profile bots; open-risk cap ignored pending orders and counted stop-less positions as $0 | Account-scoped unconditionally; pending included; stop-less = full per-trade risk |
| H3 | Risk | Risk-engine notional for FX was `lot × price` (EURUSD 1 lot = $1.10 instead of $110 k) → CVaR/leverage checks inert for FX | `instruments.notional_usd`; pip-value based bounded tail |
| H4 | Sizing | Lots rounded to nearest (0.016 → 0.02 = +25 % risk); scale-downs `max(0.01, round(x·s, 2))` could round *up*; no broker volume step | Floor to `volume_step`; scale-downs never round up; broker-minimum only within the documented 1.5× tolerance |
| H5 | Sizing | `*JPY` crosses (CHFJPY, CADJPY, NZDJPY) got a 0.0001 pip (TP/BE distances 100× too tight); USD-base pip values hard-coded $10 (USDCHF ≈ $12.5) | All `*JPY` → 0.01; price-aware pip value for USD-base pairs |
| H6 | Execution | Unparseable heartbeat / missing quote skipped freshness and deviation checks; no explicit SL/TP side check | Fail closed; `sl_tp_side_block` (BUY sl<entry<tp, SELL tp<entry<sl) |
| H7 | Forecast | Forecast-suggested stop could land on the wrong side of entry | Only accepted between current SL and entry |
| H8 | Backtest/tuning | Intraday features (session VWAP, day range) keyed on the **wall-clock date** → in replays they were empty and the Bayesian optimiser/shadow lab tuned a strategy that doesn't exist live | Anchored to the evaluated bar (`intraday_features.py`) |
| H9 | ML | Training features used the **trailed** stop-loss (only tightens on winners) → target leakage + train/serve skew | Entry-time SL (`original_stop_loss`) |
| H10 | Data | Candle reads not scoped by user → features from **another tenant's broker feed** | `user_id` threaded through `analyze_symbol` → candle fetches |
| H11 | Signals | Swing high/low included the still-forming M15 bar | Completed bars only |
| H12 | LLM | News-sentiment exceptions (missing key, bad JSON) propagated into `analyze_symbol` → **signal generation failed for every user**, uncached, retried every tick | Fully contained, neutral fallback cached 5 min |
| H13 | LLM | Headlines from open web feeds went raw into prompts that can veto trades / raise confidence (prompt injection) | `<untrusted_data>` fencing; ≥2 independent sources required before news leaves neutral; index de-dup; NaN-safe clamps |
| H14 | LLM | Loss-advisor measures (auto-applied as live guards) were not range-checked | Whitelisted types, clamped params, LLM evidence discarded, guards blocking >40 % of trades never auto-apply |
| H15 | Security | WebAuthn origin/RP ID taken from the request body + enrolment with only a session → stolen admin session could enrol a passkey and mint step-up tokens | Server-derived origin; enrolment needs step-up (or current password) |
| H16 | Security | `/safety-blocks/apply-suggestion`, onboarding risk and NL confirm / strategy-apply **bypassed step-up** for live activation & risk raises | Typed validation + the same readiness/step-up gates as `PUT /bot/config`; NL strategies default `auto_execute=false` |
| H17 | Security | Telegram webhook authenticated only by a URL-path secret (logged); `/run` enabled every bot with no checks | `X-Telegram-Bot-Api-Secret-Token` verified for webhooks registered with it; `/run` refuses suspended users and live accounts |
| H18 | Workers | Lease keeper kept trading when renewals kept failing → **two leaders** after a partition | Stops loops once renew failures exceed TTL − margin |

## 2. Medium / low — fixed

- Wilder RSI (was Cutler, and 100 for a flat series); Kalman covariance update order; embargoed time-series CV in the GBM ensemble; volatility-scaled regime pip for gold (`indicators.py`, `kalman.py`, `ml_ensemble.py`, `market_regime.py`).
- Zero/negative equity now trips the circuit breaker instead of silently passing.
- 2FA-disable failure lockout; constant-time login for unknown emails; API keys of suspended owners rejected.
- Telegram bot tokens no longer written to logs via httpx INFO URL logging.
- External release signing moved off the event loop on the async paths.
- LLM: hot-path timeouts (`LLM_HOT_PATH_TIMEOUT_S`), narration output type-checked, optimizer no longer pays a second frontier-model call on a parse error, fed-tone fence stripping no longer deletes "json" from content.
- Indexes for scalp-model/model-task queries, per-tenant candles and base-symbol risk queries (`seed.py`; regenerate `docs/INDEX_MANIFEST.md` against a live DB).

## 3. LLM models — centralised and upgraded

All 15 call sites hard-coded `claude-sonnet-4-5-20250929`. They now resolve
through `backend/llm_models.py`:

| Tier | Default | Features |
|------|---------|----------|
| fast | `claude-haiku-4-5` | news sentiment, news understanding, fed tone, signal narration, self-evaluation, journal cards |
| analysis | `claude-sonnet-5-5` | post-mortems, NL commander, strategy codegen, copilot, bot doctor, weekly insights |
| deep | `claude-opus-5-5` | loss advisor (auto-applied), hypothesis generator |
| optimizer | `claude-fable-5-1` → fallback `claude-opus-5-5` | AI optimizer (keeps the owner's Fable-tier choice) |

Overrides: `LLM_MODEL_<FEATURE>` beats `LLM_MODEL_<TIER>` beats the default.
**Roll-out note:** the `emergentintegrations` wrapper is not publicly
installable, so the parameters it sends could not be inspected. Current models
reject non-default `temperature`/`top_p`, `budget_tokens` and assistant
prefill. Verify one call per tier in staging; to revert instantly set
`LLM_MODEL_FAST=LLM_MODEL_ANALYSIS=LLM_MODEL_DEEP=claude-sonnet-4-5-20250929`.

## 4. Open items — reviewed, NOT changed in this pass (need a decision or a larger change)

| # | Area | Finding | Why not auto-fixed | Recommended action |
|---|------|---------|--------------------|--------------------|
| O1 | Crypto bridge | Bot crypto orders have **no exchange-side stop-loss**, no lifecycle management and no reconciliation; positions stay `open` forever and occupy a concurrency slot | Needs a new subsystem (OCO/stop placement, fill tracking, reconcile sweep) | Keep `BINANCE_LIVE_ENABLED=false` until a crypto lifecycle exists; route crypto through `submit_intent` |
| O2 | MT5 EA | Close / modify / partial-close paths never check `POSITION_MAGIC` — a backend ticket bug could touch manual trades | MQL5 must be compiled & validated with MetaEditor (not possible here) | Refuse when magic ≠ `MagicNumber` unless the command carries an explicit `manage_external` flag |
| O3 | MT5 EA | Crash recovery (`FindPositionForTrade`) binds by symbol/side/volume within 2 h — can attach to the wrong same-lot scalp | Same as above | Journal order ticket + send time before `OrderSend`; require a unique match within seconds |
| O4 | MT5 EA | Prices rounded to `digits`, not `SYMBOL_TRADE_TICK_SIZE`; lots rounded half-up | Same as above | `MathRound(p/ts)*ts`; `MathFloor` on lots |
| O5 | MT5 EA | Synchronous `WebRequest` 10 s timeouts in series (≈60 s blocked timer during outages); tick watermark advances before POST succeeds | Same as above | 2–3 s timeouts, failure breaker, advance watermark only on 2xx; also enforce `expires_at_ms` + `max_deviation` sent by the backend |
| O6 | Scalp engine | Quote freshness learns steady transport lag as "clock drift" (8 s-old quotes pass a 2.5 s limit) | Needs an EA protocol change (separate send timestamp) | Send `TimeGMT()`-based send time; measure against NTP-synced server clock |
| O7 | Scalp engine | `/scalp/model/retrain` runs CPU-heavy numpy on the API event loop and is callable by any user | Product decision on who may retrain a *shared* model | Admin-only, `asyncio.to_thread`, single-flight per model key |
| O8 | Ops | `METRICS_TOKEN` (Prometheus scraper credential) also authorises release promote/rollback, chaos drills | Requires new secret provisioning + deploy change | Separate `OPS_DEPLOY_TOKEN` for mutating machine paths |
| O9 | Deploy | Behind Caddy, every client shares one rate-limit IP (Caddy's) → login lockout DoS, global register/bridge limits | nginx/Caddy topology change | nginx `set_real_ip_from <caddy subnet>; real_ip_header X-Forwarded-For;` |
| O10 | Auth | Access JWTs can't be revoked (logout / password change leave a 30-min window); WebSocket doesn't check suspension | Touches every authenticated request | `sid` claim checked against session revocation, or `tokens_valid_after` per user |
| O11 | Auth | Bridge tokens stored in plaintext (agent & step-up tokens are already hashed) | Needs a data migration | Store `sha256(token)`, look up by hash |
| O12 | Workers | `BACKGROUND_WORKERS_IN_PROCESS=true` on >1 API replica runs trading loops twice (no lease) | Deployment-mode decision | Run in-process loops under `workers.base` leases or refuse when replicas>1 |
| O13 | Workers | 7 fire-and-forget `create_task`s in `server.py` without references/supervision/cancellation | Low risk, wide change | Task registry + `_supervise` + cancel on shutdown |
| O14 | Data | Daily FX history comes from ECB reference fixes (O=H=L=C) and gold from GC=F futures | Data-vendor decision | Source D1 OHLC from the broker via the EA |
| O15 | Quant | Bayes-opt / nightly tuner / strategy optimizer select in-sample, no costs, ≥5 trades | Needs the validation framework below | See §5 (purged CV, DSR, PBO, cost model) |
| O16 | Hygiene | 2 054 ruff findings (1 547 E402, 262 unused imports); CI lints only `E9,F63,F7,F82` with unpinned ruff | Mechanical, large diff — better as its own PR | Pin ruff, `--fix` F401/F541, enforce F841 + bugbear |
| O17 | Platform | Node 20 (EOL 2026-04) in `Dockerfile.frontend` + CI; uvicorn 0.25 (2023); Motor is deprecated in favour of PyMongo's native async API; `tz_aware` not set on the Mongo client | Upgrade PRs need full regression runs | Node 22 LTS; uvicorn ≥0.3x; plan Motor → `pymongo.AsyncMongoClient(tz_aware=True)` |
| O18 | Supply chain | Base images pinned by tag not digest; signer image runs as root; migrator mounts the Docker socket; `deploy-production.yml` uses `ssh-keyscan` (TOFU) | Deploy pipeline change | Digest pins, non-root signer, `DEPLOY_KNOWN_HOSTS` secret, explicit `permissions:` blocks |

## 5. Modernisation roadmap ("latest technology", ranked by expected impact)

### 5.1 LLM layer — move to the official Anthropic SDK
The model IDs are now centralised (`backend/llm_models.py`), which was the
prerequisite. Next step (≈2–3 days):

1. Add `anthropic` (AsyncAnthropic) to `requirements/llm.txt`; keep
   `emergentintegrations` for Stripe and behind `LLM_BACKEND=emergent|anthropic`
   during rollout (`EMERGENT_LLM_KEY` is a proxy key — an `ANTHROPIC_API_KEY`
   and billing are needed).
2. One `llm_client.complete(feature, system, user, schema=PydanticModel, untrusted=[...])`
   that **never raises** (returns `ok=False` = "no opinion"), uses
   **structured outputs** (`output_config.format` with a JSON schema) instead of
   the ~8 hand-rolled `find('{')`/regex JSON parsers, handles
   `stop_reason == "refusal"` / `"max_tokens"`, applies prompt caching to the
   large static system prompts, and records real `usage` tokens/cost.
3. Current models need no `temperature`/`top_p`/`budget_tokens`/assistant
   prefill (they are rejected); depth is controlled with `output_config.effort`
   (`low` for the hot-path classifiers, `high` for loss_advisor / research).
4. Batch API (50 % cheaper) for the nightly sweeps: self-evaluation lessons,
   post-mortems, hypothesis generation.

### 5.2 Validation framework (the biggest quant gap)
`scalp/model.py` already does it right — lift it into a shared `validation.py`:
- **Purged + embargoed k-fold and Combinatorial Purged CV (CPCV)** for every
  trainer (`ml_ensemble`, `learning_pipeline`, `learned_meta`, `bayes_opt`).
- **Deflated Sharpe Ratio and Probability of Backtest Overfitting (PBO)** as
  promotion gates in `nightly_tuner`, `strategy_optimizer`,
  `model_shadow.promotion_status` (log every trial; require DSR > 0.95, PBO < 0.2).
- **Cost model in every optimiser**: `cost_r = (spread + 2·slippage) / sl_dist`.
- Out-of-sample holdout for every selection step; schedule retrains instead of
  re-testing every 5 trades against a fixed 0.55 floor.

### 5.3 Uncertainty & regime
- **Conformal prediction** (split-conformal / adaptive conformal inference) on
  the Chronos quantiles in `forecast_agent.py` / `prob_forecast.py`; gate on
  conformal intervals rather than raw deciles.
- Stationary-block or GARCH-filtered bootstrap in `monte_carlo.py`
  (volatility clustering).
- Replace heuristic regime scores with a sticky HMM or Bayesian online
  change-point detection on ATR-normalised returns; hierarchical shrinkage for
  the strategy × regime edge cells instead of a hard t ≤ −1 on 8 trades.
- Evaluate Chronos-2 / TimesFM-2.5 with covariates (session, ATR regime) using
  rolling CRPS/pinball loss before increasing the transformer's ensemble weight.

### 5.4 Meta-labelling done properly
Triple-barrier labels on **all** primary signals (including vetoed ones,
recorded as shadow outcomes like `scalp_decisions` already does) to remove the
selection bias of training only on gate-passing trades; uniqueness sample
weights; isotonic / Venn–Abers calibration on a dedicated block; size from
calibrated p.

### 5.5 Architecture
1. **One instrument-spec service** fed by EA-reported `contract_size`,
   `tick_value`, `volume_step/min/max`, `stops_level` + live FX conversion —
   replaces the duplicated hard-coded tables behind several findings
   (`pip_utils`, `risk_engine._notional`, `100 if XAU` copies).
2. **Fail-closed by default** for capital-protecting overlays, enforced by a
   decorator rather than ad-hoc `try/except: fail-open`.
3. **Every engine (paper, crypto) through `submit_intent`** with atomic
   per-account exposure reservation (`$inc` under a guard) instead of
   count-then-insert.
4. **Execution contract backend ↔ EA**: every command carries `expires_at_ms`,
   `max_deviation` and an ownership flag that the EA enforces.
5. **Fenced leases everywhere**: server-time TTL + epoch checked on
   order-creating writes.
6. Event-loop lag metric in `runtime_watchdog` + a lint rule (flake8-async)
   against sync I/O in `async def`.

## 6. Behaviour changes to watch after deploy

- **More trades skipped by design** (fail-closed): missing live quote,
  unparseable heartbeat, risk-engine/clamp errors, zero equity, wrong-side
  SL/TP. Watch the SKIP/BLOCKED pulse rate for the first sessions.
- **FX sizing**: with the true FX notional the dynamic leverage cap
  (equity × 20) now binds on small accounts.
- **RSI values shift** slightly (Wilder smoothing) — re-check any RSI
  thresholds tuned on the old values.
- **News**: a single source can no longer veto entries or lift confidence.
- **Telegram**: re-enable the webhook per user to activate header
  verification; `/run` now refuses when a live account is involved.
- **Passkeys**: set `WEBAUTHN_ORIGIN` (or a correct `CORS_ORIGINS`) in
  production; the enrolment step-up dialog reuses the "API key" action label.
- **Stale pending opens** are cancelled with `close_reason: stale_pending`;
  dispatched-but-unconfirmed orders stay pending (may have filled) — decide on
  a cleanup policy.
- **Old EAs (< v1.26)** reporting inconsistent ticket counts are no longer
  auto-reconciled on heartbeat; use Force Sync.
- `scripts/test_unit.sh` compares pytest's collected count (with parametrised
  cases) to the manifest's per-`def` count; this mismatch predates the review
  (646 vs 743 on the original tree) — CI's `generate_test_manifest.py --check`
  is the authoritative gate and passes.
