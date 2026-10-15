# Security Audit #36 — v1.60.47 → v1.60.49 diff + regression sweep

**Verdict: PASS — NO MAJOR ISSUE FOUND (no P0–P2).** Scope: CI `protected-paths` guard + `scripts/check_protected_paths.py`,
`POST /nl/strategy/apply` (preview / confirm_multi / undo snapshot), `config_promotion.field_sources`, Bot Config PATCH
path, `command_fence.is_stale_ack` / `ack_type_mismatch` + bridge modification-ack, `intelligence_counters` slug check,
`market.get_quote` retry/stale cache, Strategy-agent timeout, `deferred_shadow` broker-offset path + relabel script in
the image, `/agents/macro` REAL10Y, frontend diff/flag rendering, plus a sweep of JWT/auth, admin gating, CORS, secrets.
Live unauthenticated probes: `POST /api/nl/strategy/apply` → 401, `GET /api/ops/ea-fleet` → 403,
`GET /api/agents/macro` → 401, `GET /api/bot/config` → 401.

## Verified controls
- Strategy deploy — `update_fields` is a fixed server-side key set with clamped ints (concurrency 1–10, day cap 0–20);
  `compiled` cannot mass-assign; every target mode (`default`/specific/`matching`/`all`) is filtered on `user_id`
  (`proposal_targeting.resolve_target_configs`), a specific target is `{user_id, account_id}` — no cross-tenant reach.
- No Mongo field-name injection — `field_sources.<k>` keys come only from the server `update` dict (Pydantic
  `BotConfigUpdate(exclude_unset)`); `intelligence_counters.increment` enforces `^[a-z][a-z0-9_]{1,63}$` + reserved-key
  exclusion and all callers pass constants.
- EA acks — the account is resolved from the bridge token and a trade of another account is rejected; stale / wrong-type
  acks are ignored; the only remap (FULL_CLOSE→PARTIAL_CLOSE) requires a matching `intent_id`.
- `deferred_shadow` — `$regex` input goes through `re.escape`, all queries `user_id`-scoped; `market._LAST_GOOD` is keyed
  by `SYMBOL_MAP` symbols only (bounded).
- Secrets — `/agents/macro` is auth-gated; `macro.tips` returns a transport label, never `FRED_API_KEY`; relabel script
  reads `backend/.env` at runtime, nothing baked into the image.
- CI — untrusted PR SHAs reach the guard via `env:` (never inlined `${{ }}` in `run:`); git is invoked with argument
  lists, no shell.
- Frontend — diff values, invalid flags, engine-rules panel and REAL10Y tile render as JSX text; no `dangerouslySetInnerHTML`.

## Hardening (checked)
- `price_ticks` growth: a 7-day TTL index on `ts` plus `(symbol, ts)` already exists (`seed.py:535`). No change needed.

## Not exercised
- Authenticated mutation / dynamic exploitation (read-only audit policy); covered by source review of the scoping paths.
