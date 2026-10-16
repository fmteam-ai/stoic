"""#74 — live model labels (no hard-coded "Claude Sonnet 4.5") + Bot Doctor gets the per-terminal EA build explicitly."""
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend" / "src"


def test_pretty_names_follow_llm_models():
    from llm_models import pretty_name, pretty_models, model_for
    assert pretty_name("claude-sonnet-5-5") == "Claude Sonnet 5.5"
    assert pretty_name("claude-haiku-4-5-20251001") == "Claude Haiku 4.5"
    assert pretty_name("claude-fable-5-1") == "Claude Fable 5.1"
    assert pretty_name("") == "Claude"
    assert pretty_models()["analysis"] == pretty_name(model_for("analysis"))


def test_no_hard_coded_model_labels_left():
    hits = []
    for p in list(FRONTEND.rglob("*.jsx")) + list(FRONTEND.rglob("*.js")):
        if "Sonnet 4.5" in p.read_text(encoding="utf-8", errors="ignore"):
            hits.append(str(p.relative_to(ROOT)))
    for p in BACKEND.rglob("*.py"):
        if any(part in ("tests", "__pycache__") for part in p.relative_to(BACKEND).parts):
            continue
        if re.search(r"Sonnet 4\.5", p.read_text(encoding="utf-8", errors="ignore")):
            hits.append(str(p.relative_to(ROOT)))
    assert hits == [], hits
    for f in ("components/BotDoctorPanel.jsx", "components/WeeklyDigestPanel.jsx", "pages/Agents.jsx", "pages/FAQ.jsx", "pages/Guide.jsx"):
        assert "useLlmModel(" in (FRONTEND / f).read_text(), f
    hook = (FRONTEND / "hooks" / "useLlmModel.js").read_text()
    assert '"/diagnostic/llm-models"' in hook and "models_pretty" in hook
    diag = (BACKEND / "routes" / "diagnostic_routes.py").read_text()
    assert '@router.get("/llm-models")' in diag and '"models_pretty"' in diag


def test_bot_doctor_ea_fleet_is_explicit_per_terminal():
    import bot_doctor
    now = datetime(2025, 10, 17, 12, 0, tzinfo=timezone.utc)
    accounts = [
        {"label": "RoboForex", "ea_version": "1.65", "ea_version_updated_at": "2025-10-17T11:59:00+00:00",
         "last_heartbeat": "2025-10-17T11:59:30+00:00", "trading_enabled": True, "ea_binary_sha256": "AA"},
        {"label": "OnEquity", "ea_version": "1.64", "last_heartbeat": "2025-10-17T11:50:00+00:00", "trading_enabled": True,
         "ea_binary_sha256": "bb"},
        {"label": "Tauro", "last_heartbeat": None, "trading_enabled": False},
        {"label": "Paper", "mode": "paper", "ea_version": "1.65"},
    ]
    rec = {"version": "1.65", "ex5_sha256": "aa", "previous": {"version": "1.64", "ex5_sha256": "bb"}}
    with patch("ea_capabilities._signed_release_record", lambda: rec), patch("ea_capabilities.accepted_ea_sha256s", lambda: ["aa", "bb"]):
        fleet = bot_doctor._ea_fleet(accounts, now)
    assert fleet["shipped_version"] == "1.65" and fleet["previous_version"] == "1.64"
    by = {t["label"]: t for t in fleet["terminals"]}
    assert set(by) == {"RoboForex", "OnEquity", "Tauro"}          # paper excluded
    assert by["RoboForex"]["ea_version"] == "1.65" and by["RoboForex"]["build"] == "current" and by["RoboForex"]["ex5_accepted"] is True
    assert by["RoboForex"]["heartbeat_age_seconds"] == 30
    assert by["OnEquity"]["ea_version"] == "1.64" and by["OnEquity"]["build"] == "previous"
    assert by["Tauro"]["ea_version"] is None and by["Tauro"]["build"] == "unknown" and by["Tauro"]["ex5_accepted"] is None
    assert fleet["terminals_by_version"] == {"1.65": ["RoboForex"], "1.64": ["OnEquity"], "unreported": ["Tauro"]}
    assert "ea_fleet.terminals[*].ea_version" in bot_doctor._DOCTOR_SYSTEM
    src = (BACKEND / "bot_doctor.py").read_text()
    assert '"ea_fleet": ea_fleet,' in src and '"ea_version_updated_at": 1' in src
