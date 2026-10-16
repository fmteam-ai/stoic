"""v1.60.59 — llm_client: direct Anthropic when ANTHROPIC_API_KEY is set, Emergent Universal Key otherwise, fail closed
without either; keys never leak; per-tier max_tokens caps + per-feature daily counters; Co-Pilot history replay;
news-veto visibility; Integrations test button; every call site goes through the helper."""
import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

import llm_client  # noqa: E402

BACKEND = Path(__file__).resolve().parents[2]
DIRECT_KEY = "sk-ant-api03-DIRECTSECRETKEY0000000000000000"
EMERGENT_KEY = "sk-emergent-UNIVERSALSECRET000"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EMERGENT_LLM_KEY", raising=False)
    llm_client._counters.clear()
    for k in llm_client._status:
        llm_client._status[k] = None
    monkeypatch.setattr(llm_client, "_record_bg", lambda *a, **k: None, raising=False)
    with patch("llm_timeout._record_bg", lambda *a, **k: None):
        yield


# ── provider selection ───────────────────────────────────────────────────────────────────────────────

def test_direct_anthropic_wins_when_key_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", DIRECT_KEY)
    monkeypatch.setenv("EMERGENT_LLM_KEY", EMERGENT_KEY)
    assert llm_client.active_provider() == "anthropic_direct"
    direct, emergent = AsyncMock(return_value="OK direct"), AsyncMock(return_value="OK emergent")
    with patch.object(llm_client, "_direct", direct), patch.object(llm_client, "_emergent", emergent):
        out = _run(llm_client.complete("fast", "sys", "Say OK", feature="t"))
    assert out == "OK direct" and emergent.await_count == 0
    model, system, user_text, hist, max_tokens = direct.await_args.args
    assert model == llm_client.model_for("fast") and system == "sys" and user_text == "Say OK" and hist == []
    assert max_tokens == llm_client.MAX_TOKENS["fast"]
    assert llm_client.status()["features_today"]["t"] == {"calls": 1, "ok": 1, "errors": 0, "provider": "anthropic_direct"}


def test_falls_back_to_emergent_without_direct_key(monkeypatch):
    monkeypatch.setenv("EMERGENT_LLM_KEY", EMERGENT_KEY)
    assert llm_client.active_provider() == "emergent"
    emergent = AsyncMock(return_value="OK emergent")
    with patch.object(llm_client, "_direct", AsyncMock(side_effect=AssertionError("must not be called"))), \
            patch.object(llm_client, "_emergent", emergent):
        assert _run(llm_client.complete("analysis", "sys", "hi", feature="copilot", max_tokens=99999)) == "OK emergent"
    args = emergent.await_args.args
    assert args[0] == llm_client.model_for("analysis") and args[1] == "anthropic"
    assert args[5] == llm_client.MAX_TOKENS["analysis"], "caller may not exceed the per-tier cap"


def test_no_credential_fails_closed():
    assert llm_client.active_provider() == "none" and not llm_client.configured()
    with pytest.raises(llm_client.LlmUnavailable) as ei:
        _run(llm_client.complete("fast", "s", "u", feature="t"))
    assert ei.value.kind == "auth"


def test_tier_caps_and_daily_cap(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", DIRECT_KEY)
    assert llm_client.MAX_TOKENS == {"fast": 1024, "analysis": 2048, "deep": 8192, "ai_optimizer": 4096, "ai_optimizer_fallback": 4096}
    monkeypatch.setattr(llm_client, "DAILY_CALL_CAP", 2)
    with patch.object(llm_client, "_direct", AsyncMock(return_value="x")):
        _run(llm_client.complete("fast", "s", "u", feature="a"))
        _run(llm_client.complete("fast", "s", "u", feature="b"))
        with pytest.raises(llm_client.LlmUnavailable) as ei:
            _run(llm_client.complete("fast", "s", "u", feature="c"))
    assert ei.value.kind == "daily_cap" and llm_client.status()["today_calls"] == 2


# ── errors / secrecy ─────────────────────────────────────────────────────────────────────────────────

class _HttpErr(Exception):
    def __init__(self, status_code, msg):
        super().__init__(msg)
        self.status_code = status_code


def test_emergent_403_is_reported_as_host_blocked_not_bad_key(monkeypatch, caplog):
    monkeypatch.setenv("EMERGENT_LLM_KEY", EMERGENT_KEY)
    html = f"<html><body>403 Forbidden</body></html> key={EMERGENT_KEY}"
    with patch.object(llm_client, "_emergent", AsyncMock(side_effect=_HttpErr(403, html))), caplog.at_level(logging.WARNING):
        with pytest.raises(_HttpErr):
            _run(llm_client.complete("fast", "s", "u", feature="news"))
    st = llm_client.status()
    assert st["last_error_kind"] == "universal_key_blocked_offhost"
    assert "not allowed from this host" in st["last_error"]
    assert EMERGENT_KEY not in caplog.text and EMERGENT_KEY not in str(st)
    assert st["features_today"]["news"]["errors"] == 1
    # the same 403 on the direct path is a credential problem, not a host block
    assert llm_client.classify_error(_HttpErr(403, "forbidden"), "anthropic_direct")[0] == "auth"
    assert llm_client.classify_error(_HttpErr(404, "model_not_found"), "anthropic_direct")[0] == "model_not_found"
    assert llm_client.classify_error(asyncio.TimeoutError(), "anthropic_direct")[0] == "timeout"


def test_keys_never_in_status_or_redacted_text(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", DIRECT_KEY)
    monkeypatch.setenv("EMERGENT_LLM_KEY", EMERGENT_KEY)
    assert DIRECT_KEY not in str(llm_client.status()) and EMERGENT_KEY not in str(llm_client.status())
    red = llm_client.redact(f"auth failed for {DIRECT_KEY} and {EMERGENT_KEY} and sk-other-ABCDEFGHIJ")
    assert DIRECT_KEY not in red and EMERGENT_KEY not in red and "sk-other-ABCDEFGHIJ" not in red


def test_integrations_ai_test_button_makes_one_tiny_call_and_hides_the_key(monkeypatch):
    import integrations_settings as iset
    monkeypatch.setenv("ANTHROPIC_API_KEY", DIRECT_KEY)
    assert iset.REGISTRY["ANTHROPIC_API_KEY"][:2] == ("ai", True) and iset.REGISTRY["EMERGENT_LLM_KEY"][:2] == ("ai", True)
    direct = AsyncMock(return_value="OK")
    with patch.object(llm_client, "_direct", direct):
        res = _run(iset.test_provider(FakeDb(), "ai", {"id": "u1", "email": "a@b"}))
    assert res["ok"] is True and res["provider"] == "anthropic_direct" and direct.await_args.args[4] == 8
    assert DIRECT_KEY not in str(res)
    with patch.object(llm_client, "_direct", AsyncMock(side_effect=_HttpErr(401, f"invalid x-api-key {DIRECT_KEY}"))):
        res = _run(iset.test_provider(FakeDb(), "ai", {"id": "u1", "email": "a@b"}))
    assert res["ok"] is False and res["error_kind"] == "auth" and DIRECT_KEY not in str(res)


# ── Co-Pilot memory ──────────────────────────────────────────────────────────────────────────────────

def test_copilot_sends_last_six_turns_from_db_history(monkeypatch):
    import copilot
    monkeypatch.setenv("ANTHROPIC_API_KEY", DIRECT_KEY)
    db = FakeDb()
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(10)]
    db.copilot_sessions.rows.append({"user_id": "u1", "session_id": "s1", "messages": list(msgs)})
    assert copilot.copilot_history({"messages": msgs}) == msgs[-6:]
    monkeypatch.setattr(copilot, "get_db", lambda: db)
    complete = AsyncMock(return_value="answer")
    with patch.object(llm_client, "complete", complete):
        out = _run(copilot.chat("u1", "and now?", "s1"))
    assert out["answer"] == "answer" and out["session_id"] == "s1"
    kw = complete.await_args.kwargs
    assert kw["feature"] == "copilot" and kw["history"] == msgs[-6:]
    assert complete.await_args.args[0] == "analysis" and complete.await_args.args[2] == "and now?"
    assert len(db.copilot_sessions.rows[0]["messages"]) == 12       # user + assistant appended
    # the direct path really sends the history (alternating roles) ahead of the new message
    direct = AsyncMock()

    class _Resp:
        content = [type("B", (), {"type": "text", "text": "ok"})()]
    direct.return_value = _Resp()
    client = type("C", (), {"messages": type("M", (), {"create": direct})(), "close": AsyncMock()})()
    with patch("anthropic.AsyncAnthropic", lambda **kw: client):
        assert _run(llm_client._direct("m", "sys", "q", llm_client._history_messages(msgs[-6:]), 50)) == "ok"
    sent = direct.await_args.kwargs["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant"] * 3 + ["user"] and sent[-1]["content"] == "q"
    assert direct.await_args.kwargs["max_tokens"] == 50 and direct.await_args.kwargs["system"] == "sys"


# ── news veto visibility ─────────────────────────────────────────────────────────────────────────────

def test_news_veto_inactive_note_when_sentiment_empty():
    from ai_signals import news_veto_state, NEWS_VETO_INACTIVE_NOTE
    from news import unavailable_sentiment
    st = news_veto_state(unavailable_sentiment("XAUUSD"), mtf_mode=True)
    assert st == {"active": False, "mode": "hard", "note": NEWS_VETO_INACTIVE_NOTE, "why": st["why"]}
    assert news_veto_state({"available": True, "score": -0.7}, mtf_mode=False) == {"active": True, "mode": "advisory", "note": ""}
    src = (BACKEND / "ai_signals.py").read_text()
    assert 'key_factors = [*key_factors, news_veto["note"]]' in src and '"news_veto": news_veto,' in src
    diag = (BACKEND / "routes" / "diagnostic_routes.py").read_text()
    assert "news veto inactive — LLM unavailable" in diag and '@router.get("/llm-status")' in diag


# ── single entry point ───────────────────────────────────────────────────────────────────────────────

def test_no_call_site_builds_its_own_chat_client():
    offenders = []
    for p in BACKEND.rglob("*.py"):
        rel = p.relative_to(BACKEND).parts
        if any(part in ("tests", "scripts", "__pycache__", "node_modules", ".venv", "venv") for part in rel) or p.name == "llm_client.py":
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if p.name != "llm_models.py" and re.search(r"LlmChat\(", txt) or re.search(r'os\.environ\[\s*"EMERGENT_LLM_KEY"\s*\]', txt):
            offenders.append(str(p.relative_to(BACKEND)))
    assert offenders == [], offenders
    assert "claude-fable-5-1" in llm_client.model_for("ai_optimizer") and llm_client.MAX_TOKENS["fast"] <= 1024
    assert "news tagging stays on \"fast\"" in (BACKEND / "news_understanding.py").read_text()
