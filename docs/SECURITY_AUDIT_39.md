# Security Audit #39 — v1.60.59 → v1.60.61 + hotfix (LLM provider layer, Gold bias / TP1 bank, #78–#81, EA release chain) + regression sweep

**Verdict: PASS / NO MATERIAL FUNCTIONAL ISSUE FOUND** (read-only audit against the deployed preview, 2026-10-17). No P0–P2.

## Scope
- **LLM layer** (`llm_client.py`): direct Anthropic vs Emergent key selection, key redaction in errors/logs, per-tier `max_tokens`
  caps, `LLM_DAILY_CALL_CAP`, per-feature counters. `/api/diagnostic/llm-status` admin (`require_admin`), `/api/diagnostic/llm-models`
  signed-in only — returns ids / pretty names / provider, never keys. `integrations_settings.py` `ANTHROPIC_API_KEY` sealed
  (AES-GCM), Test button never logs/returns the key. Co-Pilot history (`db.copilot_sessions`) scoped per user.
- **v1.60.60**: `gold_bias.py` (reads only server-written caches), `exit_policy_tp1.py` + `trade_manager._tp1_bank_runner_exits`
  (every pending modification fenced via `command_fence`; `trail_sl` never loosens), preset apply through
  `config_promotion.apply_config_change` (#78: version + audit), `news_understanding` #79 (headlines are data; 200-char cap; strict
  `[{i, score, why}]` schema or reject all), `market_intel` hrefs http(s)-only.
- **v1.60.61**: `bridge_routes.receive_candles` (bridge-token auth via hashed match, 4 MB body cap, H4/D1 400-bar cap, 150-day window,
  broker-grid alignment), `poll_trades` `candles_request` (fail-soft block only adds a fixed symbol/timeframe/bars triple, no account
  data), `modification_ack` follow-up `new_tp` (own account/token only; loosened broker stops flagged), `gold_h4_history.py`
  (`accounts.h4_history_request` written by `_id` of the authenticated terminal), BotConfig static text (React-escaped), EA 1.66
  `ParseCandlesRequest` (server-controlled symbol/timeframe/bars: bars clamped 50–400, timeframe mapped through a fixed enum, 60 s local
  flood guard, same `/api/bridge/candles` contract).
- **Release chain**: `verify_ea_release.py` forward-bump adoption requires the sanctioned CI job + external signer secrets + repo write
  (same-version drift refused); `check_release_hash_drift.py`; new `check_ea_commit_consistency.py`; `ea-release.yml` (`$ErrorActionPreference='Stop'`,
  `$LASTEXITCODE` checks, UTF-8 env, `contents: write` scoped to the bot commit-back).
- **Live sweep** (browser UA): `/api/diagnostic/llm-status|llm-models|run`, `/api/market-intel/gold-bias`, `/api/scalp/exit-rules` → 401
  unauthenticated; `/api/bridge/candles` and `/api/bridge/poll-trades` with a bad token → 401, no data; security headers unchanged
  (CSP `default-src 'none'`, HSTS, `X-Frame-Options: DENY`, nosniff, Referrer-Policy); bridge IP limit 1200/min; no stack traces.

## P3 (hardening)
1. **ForexFactory XML — no size cap / internal-entity expansion** (`economic_calendar._fetch_events`). Stdlib ElementTree never
   resolves external entities (no XXE file read/SSRF) but expands internal ones (billion laughs) — needs a hostile/MITM'd fixed HTTPS
   upstream. **Applied**: `parse_feed_xml()` — 2 MB cap (`FF_FEED_MAX_BYTES`), any `<!DOCTYPE`/`<!ENTITY` declaration rejected before
   parsing, malformed → `None` (no raise); unit test with an entity bomb, an oversize body, malformed XML and the real feed shape.
   (No new dependency — `defusedxml` deliberately not added: `requirements.txt` is not regenerated in this repo.)
2. **Wildcard CORS, credential-less branch** — unchanged baseline (#37/#38): credentials disabled when the allowlist is empty;
   production keeps `CORS_ORIGINS` populated so credentialed calls stay on the explicit-origin branch. No change.
3. **EA-reported SL/TP adopted after a confirmed modification** (`bridge_routes.modification_ack`): a loosened broker stop is flagged
   (`ack_loosened_stop`) but stored — it reflects broker reality for the terminal's own account (own token). Noted for a later
   decision (reject vs flag); no change in this release.

## Gaps
MQ5 client-side parsing reviewed at the server-contract level (runs on the customer's terminal; the server is the trust root). CI signing
trust rests on GitHub `main` write access + external signer secrets (accepted boundary). No authenticated session used (read-only policy);
object-level isolation verified from source (`user_id` / account `_id` scoping on every new read and write). Protected paths untouched.
