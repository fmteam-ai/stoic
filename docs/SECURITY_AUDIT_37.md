# Security Audit #37 — v1.60.50 → v1.60.52 diff + regression sweep

**Verdict: PASS — NO MAJOR ISSUE FOUND (no P0–P2).** Scope: the v1.60.52 changed surfaces — global
`StaleQuoteError` → 409 handler (`server.py`), `market._stale_checked` + `allow_stale=False` in `trigger_sweeper` and the
manual order route, `bot_runner` H1-alignment gate (`mtf_check.h1_alignment`) and the `user_direction_cap` try/finally
around `engine.execute`, bridge modification-ack fence (`command_fence.ack_clears_pending`,
`is_duplicate_close_leg_ack`), `POST /nl/strategy/apply` (enum lower-casing, per-config policy cap, step-up),
`POST /bot/preset/{key}` `preset_fields`, `position_protector.sweep_all`, `deferred_shadow._m15_after`, `macro/dxy`
fallback, `strategy_presets._stricter`, `streak_losses`, `models.require_h1_alignment`, `BotConfig.jsx` toggle — plus a
sweep of auth/step-up on risk-raising routes, bridge token handling, manual-order rate limit, CORS, secrets, lint.
Live unauthenticated probes: `POST /api/nl/strategy/apply` → 401, `GET /api/bot/config` → 401,
`POST /api/trades/manual` → 401.

## Verified controls
- Stale-quote 409 — the handler is type-specific; on every trading route the auth `HTTPException` is raised before
  `get_quote`, so the 409 can never mask a login/step-up failure. `reason` is `str(exc)[:200]` and the message carries only
  a normalised symbol + age.
- `market._stale_checked` — a cached stale copy is dropped once the last good quote passes `STALE_MAX_AGE_SEC` (TTL
  clipped to the remaining age); `allow_stale=False` callers get `StaleQuoteError`. `_LAST_GOOD` / `_cache` keyed by
  `SYMBOL_MAP` symbols only (bounded).
- `bot_runner` — H1 gate is fail-open by design (no higher-TF view = no veto) and only reads the signal; the direction-cap
  reservation is taken once, released in `finally` on block / stale / exception / success; the Safety-Guardian `blocked`
  branch is handled before fall-through; exactly one `engine.execute` per signal — no double-execute, no leaked slot.
- EA acks — account resolved from the bridge token and a trade of another account is rejected (unchanged); a replayed
  close-leg ack (intent already in `executed_intents`) is stale; a FAILED close-leg ack cannot clear a fenced MODIFY_SL
  owned by another intent; `executed_intents` (slice −50) dedup applies on success and failure.
- Strategy deploy — `update_fields` stays a fixed server-side key set (lower-cased enums validated → 422); the policy
  cap (`_refuse_risk_above_policy_cap`, 409) and step-up run BEFORE any write; targets are `user_id`-scoped via
  `resolve_target_configs` — no mass-assign, no cross-tenant reach.
- `/bot/preset/{key}` — auth + account-ownership gated; `preset_fields` are config key names already present in `config`.
- `position_protector.sweep_all` — `ObjectId.is_valid` guard before the `$in`; configs remain user/account scoped.
- Frontend — the new toggle renders as JSX text; `presetTouchedRef` only holds server-returned key names; no
  `dangerouslySetInnerHTML`.
- Regression — `scripts/secret_scan.py`: 0 blocking (194 historical, rotated/retired); `--select E9,F63,F7,F82,F823`
  lint clean; manual-order route keeps `check_and_record` rate limit; CORS credentialed branch uses the filtered allowlist.

## Hardening (P3, noted)
- CORS wildcard branch (`server.py` ~845) serves `*` only with `allow_credentials=False`; keep the filtered allowlist
  populated in production so the credential-less branch is never the live policy.
- Keep future `StaleQuoteError` messages free of internal detail (hosts, stack, account ids).

Protected release/policy artifacts untouched. Read-only audit — authenticated mutation not exercised (covered by source
review).
