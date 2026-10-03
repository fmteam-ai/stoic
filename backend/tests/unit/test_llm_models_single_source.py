"""Roadmap step 7 — llm_models.py is the ONLY place a model identifier may appear."""
import os
import re
from pathlib import Path

import pytest

import llm_models
from llm_models import DEFAULTS, ENV_VARS, TIERS, PROVIDER, model_for, optimizer_candidates, describe

pytestmark = pytest.mark.unit

BACKEND = Path(__file__).resolve().parents[2]
MODEL_RE = re.compile(r"""["'](claude-[a-z0-9.-]+|gpt-[0-9][a-z0-9.-]*|gemini-[a-z0-9.-]+|o[134]-[a-z0-9.-]+)["']""")
SKIP_DIRS = {"tests", "__pycache__", "node_modules", ".venv", "venv"}


def _py_files():
    for p in BACKEND.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.relative_to(BACKEND).parts):
            continue
        if p.name == "llm_models.py":
            continue
        yield p


def test_no_file_hardcodes_a_model_identifier():
    offenders = []
    for p in _py_files():
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if MODEL_RE.search(line):
                offenders.append(f"{p.relative_to(BACKEND)}:{i}: {line.strip()[:100]}")
    assert not offenders, "model identifiers must live in llm_models.py only:\n" + "\n".join(offenders)


def test_every_with_model_call_goes_through_llm_models():
    bad = []
    for p in _py_files():
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        for i, line in enumerate(lines, 1):
            if ".with_model(" not in line:
                continue
            window = " ".join(lines[i - 1:i + 2])          # call may span lines
            if "model_for(" not in window and "provider, model" not in window:
                bad.append(f"{p.relative_to(BACKEND)}:{i}: {line.strip()[:100]}")
    assert not bad, "with_model() must use llm_models.model_for():\n" + "\n".join(bad)


def test_defaults_and_env_names():
    assert set(TIERS) == {"fast", "analysis", "deep", "ai_optimizer", "ai_optimizer_fallback"}
    assert DEFAULTS["fast"].startswith("claude-haiku-4-5")
    assert DEFAULTS["analysis"] == "claude-sonnet-5-5"
    assert DEFAULTS["deep"] == "claude-opus-5-5"
    assert DEFAULTS["ai_optimizer"] == "claude-fable-5-1" and DEFAULTS["ai_optimizer_fallback"] == "claude-opus-5-5"
    assert ENV_VARS == {"fast": "LLM_MODEL_FAST", "analysis": "LLM_MODEL_ANALYSIS", "deep": "LLM_MODEL_DEEP",
                        "ai_optimizer": "LLM_MODEL_AI_OPTIMIZER",
                        "ai_optimizer_fallback": "LLM_MODEL_AI_OPTIMIZER_FALLBACK"}
    assert PROVIDER == "anthropic"


def test_env_override_wins_and_blank_is_ignored(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_ANALYSIS", "claude-opus-5-5")
    assert model_for("analysis") == "claude-opus-5-5"
    monkeypatch.setenv("LLM_MODEL_ANALYSIS", "   ")
    assert model_for("analysis") == DEFAULTS["analysis"]
    monkeypatch.delenv("LLM_MODEL_ANALYSIS", raising=False)
    assert model_for("analysis") == DEFAULTS["analysis"]
    with pytest.raises(KeyError):
        model_for("turbo")


def test_optimizer_candidates_dedupe(monkeypatch):
    monkeypatch.delenv("LLM_MODEL_AI_OPTIMIZER", raising=False)
    monkeypatch.delenv("LLM_MODEL_AI_OPTIMIZER_FALLBACK", raising=False)
    assert optimizer_candidates() == [("anthropic", "claude-fable-5-1"), ("anthropic", "claude-opus-5-5")]
    monkeypatch.setenv("LLM_MODEL_AI_OPTIMIZER", "claude-opus-5-5")
    assert optimizer_candidates() == [("anthropic", "claude-opus-5-5")]


def test_describe_reports_overrides(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_FAST", "claude-sonnet-5-5")
    d = describe()
    assert d["tiers"]["fast"]["overridden"] is True and d["tiers"]["fast"]["model"] == "claude-sonnet-5-5"
    assert d["tiers"]["deep"]["overridden"] is False
    assert os.path.basename(llm_models.__file__) == "llm_models.py"
