# PR: v1.60.59 — direct Anthropic provider (Universal Key refused off-platform), Co-Pilot memory, news-veto visibility

Base: GitHub `main` @ `347f421` (v1.60.55); stacks on `release/v1.60.58` (#57–#70). Branch `release/v1.60.59` via Save to
GitHub. No tag. No new trading flags; no default change on any account; protected paths untouched.
Notes: `docs/RELEASE_NOTES_v1.60.59.md`.

## Why
Emergent support confirmed the Universal Key answers **403 Forbidden** for every host outside Emergent (our VPS since
15 Oct 22:10 UTC). Trading was unaffected (LLM layer is advisory and fails closed), but narration, news sentiment (and with
it the dual-AI **news veto**), Fed tone, post-mortems, optimizer, Bot Doctor narrative, Co-Pilot, NL Commander, digest and
journal cards were silently off.

## What
* `backend/llm_client.py` — single entry point: `ANTHROPIC_API_KEY` → official `anthropic` SDK (direct), else
  `EMERGENT_LLM_KEY` → emergentintegrations, else fail closed. Models only from `llm_models.model_for()` (defaults verified on
  the direct API). Per-tier `max_tokens` caps, `LLM_DAILY_CALL_CAP` (2000), per-feature daily counters, key redaction,
  error classification (Emergent 403 = "Universal Key not allowed from this host").
* 16 call sites migrated; none builds its own client (test). News tagging stays on `fast`.
* Co-Pilot sends the last 6 turns from `db.copilot_sessions` with each request (direct API is stateless).
* Signals carry `news_veto` + key_factors note "news veto inactive — LLM unavailable" when sentiment is empty; Bot Health
  "News veto" check; `GET /api/diagnostic/llm-status`; new `LlmProviderCard` (provider, last error, cost counters).
* Admin → Integrations → AI: `ANTHROPIC_API_KEY` sealed secret + Test button (one tiny call, key never returned).

## Files
`llm_client.py` (new), `llm_models.py` (doc), `copilot.py`, `ai_signals.py`, `news_understanding.py`, `fed_tone.py`,
`self_evaluation.py`, `loss_postmortem.py`, `loss_advisor.py`, `ai_optimizer.py`, `bot_doctor.py`, `nl_commander.py`,
`strategy_code_generator.py`, `research_agent/hypothesis_generator.py`, `routes/{insights,journal,copilot,diagnostic}_routes.py`,
`integrations_settings.py`, `requirements.txt` (+anthropic), `frontend/src/components/LlmProviderCard.jsx`,
`frontend/src/pages/BotHealth.jsx`, tests (`test_v1_60_59_llm_client.py` 10; 3 legacy tests re-pointed to the helper), docs.

## Validation
Unit lane **1958 ✓**; `test_llm_models_single_source.py` green; secret_scan 0 blocking; protected-paths OK; artifacts
regenerated. Live preview: Integrations Test → "emergent answered “OK” via claude-haiku-4-5-20251001"; Bot Health card shows
provider/counters from the running loop (news, fed_tone, bot_doctor via the helper). Direct Anthropic path exercised with
a mocked SDK client in tests (no Anthropic key in the sandbox) — first real call happens on the VPS via the Test button.
