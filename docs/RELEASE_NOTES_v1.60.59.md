# v1.60.59 — LLM provider abstraction (direct Anthropic ⇄ Emergent Universal Key), Co-Pilot memory, news-veto visibility

Background: since 15 Oct ~22:10 UTC the Emergent Universal Key is refused with an HTML **403** for any host outside
Emergent (confirmed by Emergent support — key/plan/credits are fine). Trading was never affected: the LLM layer is
advisory/explanatory and every call site already failed closed. What was silently lost: signal narration, news
sentiment (→ the dual-AI news veto), Fed tone, post-mortems, optimizer/research suggestions, Bot Doctor narrative,
Co-Pilot, NL Commander, weekly digest, journal cards.

## What changed
* **`backend/llm_client.py` — the ONE LLM entry point.** `complete(tier, system, user_text, *, feature, history=None,
  seconds=None, max_tokens=None, model=None, provider=None) -> str`.
  * `ANTHROPIC_API_KEY` set → **direct Anthropic** via the official `anthropic` SDK (`AsyncAnthropic.messages.create`);
    else `EMERGENT_LLM_KEY` → emergentintegrations `LlmChat`; else `LlmUnavailable` (callers keep their fail-closed paths).
    Decided per call — an Admin → Integrations change applies instantly.
  * Model names still come only from `llm_models.model_for()` (single-source test unchanged). The four defaults were
    verified against the direct Anthropic API model list (16 Oct 2026): `claude-haiku-4-5-20251001` (legacy, available),
    `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-fable-5-1`.
  * Every call runs under the `llm_timeout` budget and is sampled for the AI-latency card (provider now shows
    `anthropic_direct` / `emergent`).
  * **Cost controls**: per-tier `max_tokens` caps (fast 1024 · analysis 2048 · deep 8192 · ai_optimizer 4096; callers can only
    lower them), a global `LLM_DAILY_CALL_CAP` (default 2000/UTC day, fail closed above it), per-feature daily counters
    (`llm_call_counters`, in-memory + Mongo). News tagging stays on the **fast** tier.
  * **Secrecy**: keys are never logged or returned — `redact()` strips both configured keys and `sk-…` tokens from every
    error/status string.
  * **Error classification**: a 403 from the Emergent proxy is reported as *"Universal Key not allowed from this host"*
    (`universal_key_blocked_offhost`), distinct from `auth`, `model_not_found`, `rate_limited`, `timeout`, `daily_cap`.
* **All 16 call sites** (`ai_signals` narration, `news_understanding`, `fed_tone`, `self_evaluation`, `loss_postmortem`,
  `loss_advisor`, `ai_optimizer` (tier `ai_optimizer` + fallback), `research_agent/hypothesis_generator`,
  `strategy_code_generator`, `bot_doctor`, `copilot`, `nl_commander` ×2, `routes/insights_routes`, `routes/journal_routes`)
  now call `llm_client.complete()`; none builds `LlmChat` or reads `EMERGENT_LLM_KEY` itself (test enforces).
* **Co-Pilot memory**: the direct API is stateless, so `copilot.chat` sends the last **6** turns from
  `db.copilot_sessions` with every request (`copilot_history`, 2000 chars/message; same history is passed to the Emergent
  path as initial messages; roles alternate as Anthropic requires).
* **News veto visibility**: `ai_signals.news_veto_state()` — whenever the sentiment came back empty the signal carries
  `news_veto={active:false, mode:hard|advisory, note:"news veto inactive — LLM unavailable"}` and the note is appended to
  `key_factors` (visible in the Signals notes). Bot Health "News veto" check + `GET /api/diagnostic/llm-status`.
* **Admin → Integrations → AI**: `ANTHROPIC_API_KEY` (sealed vault, category `ai`, validator `sk-ant-…`) next to
  `EMERGENT_LLM_KEY`; the **Test** button makes one tiny `fast` call (`max_tokens=8`) and reports the active provider or
  the classified error — never the key.
* **Bot Health**: new `LlmProviderCard` (active provider, models, max_tokens, today's calls/cap, last redacted error with the
  off-host 403 wording, per-feature counter table, NEWS VETO ACTIVE/INACTIVE badge); the "Claude LLM" diagnostic check now
  reports provider + last error; Co-Pilot 503 message mentions both keys.

## #74 (cosmetic) — live model labels + explicit EA fleet for Bot Doctor
* No more hard-coded "Claude Sonnet 4.5": `llm_models.pretty_name()` / `pretty_models()` turn the live tier model into a
  display name ("Claude Sonnet 5.5"); `GET /api/diagnostic/llm-models` (any signed-in user; ids + pretty names + provider,
  nothing secret) feeds the `useLlmModel(tier)` hook used by BotDoctorPanel, WeeklyDigestPanel, Agents, FAQ and Guide;
  `/diagnostic/llm-status` (admin) also carries `models_pretty`. Docstrings in bot_doctor, copilot, ai_signals, nl_commander,
  agents/strategy_agent, insights_routes, ai_optimizer now reference the tier, not a model.
* Bot Doctor telemetry gets an explicit `ea_fleet` block (shipped/previous version from the signed release record, per
  terminal: `ea_version` from the heartbeat's client_version, `ea_version_reported_at`, build current/previous/other/unknown,
  EX5 accepted, heartbeat age; paper accounts excluded; `terminals_by_version`), and the system prompt instructs the model to
  quote EA versions per label verbatim and never infer one terminal's build from another. Test: `test_v1_60_59_review_74_labels.py` (3).

## Default behaviour: unchanged on every account
No flag or default changed. **No change to trading logic, gates or guards** — deterministic engines never call the LLM;
if the LLM is unavailable every feature fails closed exactly as before (narration skipped, sentiment unavailable →
veto inactive and now *visible*, agents skipped, Co-Pilot/NL 503). New env (optional): `ANTHROPIC_API_KEY`,
`LLM_DAILY_CALL_CAP` (2000).

## Tests
`tests/unit/test_v1_60_59_llm_client.py` (10): direct-when-set / fallback / fail-closed, per-tier + daily caps, Emergent 403 →
host-blocked (not bad key) and key absent from logs/status, redaction, Integrations test button, Co-Pilot sends the last 6
turns (and the direct path really sends them, alternating), news-veto note, no call site builds its own client.
Existing `test_llm_models_single_source.py` (6) unchanged and green. Unit lane 1958 ✓.

## Operator steps (VPS)
1. Admin → Integrations → AI → paste the Anthropic key (`sk-ant-…`) → **Test** → expect "anthropic_direct answered “OK”".
2. Bot Health → LLM PROVIDER card shows DIRECT ANTHROPIC and NEWS VETO ACTIVE after the next news scoring (≤ 10 min).
Billing moves to Anthropic for that host; the Emergent key keeps working for the preview on Emergent.
