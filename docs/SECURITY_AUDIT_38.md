# Security Audit #38 — v1.60.54 surface (#39 anti-tilt helper, #40 R-multiple, #41 Market Intelligence, #42 pip-value guard) + regression sweep

**Verdict: PASS / NO MAJOR ISSUE FOUND** (read-only audit against the deployed preview, 2026-10-16). No P0–P2.

## Scope
- `GET /api/market-intel` (`routes/market_intel_routes.py`, `market_intel.py`): auth required (live 401 unauthenticated), user-scoped (`bot_configs`, `trade_decisions`, `news_ai_cache` filtered by user id), read-only (POST → 405), `scrub()` drops credential-like keys, no raw provider payloads; `$regex` built only from the fixed `INSTRUMENTS` tuple + fixed `USAGE_RE` (no user input, no nested quantifiers).
- `anti_tilt.py` + `/bot/cooldowns`, `/bot/health-score`, `/bot/pulse`, diagnostics: `streak_query(user_id, account_id)` everywhere; `anti_tilt_trades[]` carries trade id / symbol / pnl / R / why only.
- `analytics` R-multiple, `instruments.known_contract_size`, `streak_losses`, `scripts/recompute_r_multiples.py` (CLI only, dry-run default, `--apply` writes `r_multiple/r_source/r_recomputed_at`, never pnl/trading fields, not HTTP-reachable).
- `risk.resolve_pip_value` / `raw_lot_anomaly` + bot_runner wiring: fail-safe direction confirmed (anomaly → standard-contract table; raw lot > 10× max is rejected, never clamped), broker spec values cast with Type/ValueError handling, alert/log lines carry no credentials.
- `news_understanding` scored items (titles truncated 160 / why 80, no keys), `economic_calendar.get_events` shared-cache fallback reads only server-written `platform_state`.
- Frontend `MarketIntelCard.jsx`, `CooldownPanel.jsx`: React-escaped text, no `dangerouslySetInnerHTML`, external links `target=_blank rel=noreferrer`.
- Live sweep: `/api/market-intel`, `/api/bot/cooldowns`, `/api/bot/health-score` → 401; `/api/ops/risk-regime` → 403; CSP `default-src 'none'`, HSTS, `X-Frame-Options: DENY`, nosniff, Referrer-Policy unchanged.

## P3 (hardening)
1. **External-link scheme unvalidated** (feed-supplied `url`/`link` → href). **Applied**: `market_intel._safe_url()` — only `http(s)://` links leave the server (headline url + central-bank link); unit test added.
2. **Wildcard CORS** credential-less branch — unchanged from #37; credentialed branch stays on the allowlist. No change.

## Gaps
Authenticated cross-user bodies not fetched (no login in the read-only audit); isolation verified from source-level `user_id` scoping, consistent across all new endpoints. Protected paths untouched.
