"""Cap-aware presets — GET /bot/presets annotates every style with its fit under the active policy cap (same
arithmetic as the save refusal), POST /bot/preset/{key} refuses over-cap styles, and the Bot Config page renders the
verdict before the user picks."""
import asyncio
import os
import sys
import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cap(monkeypatch, cap):
    import rules_policy as rp

    async def _caps(db):
        return {"max_risk_pct": cap} if cap is not None else {}
    monkeypatch.setattr(rp, "risk_caps", _caps)


def test_preset_cap_fit_matches_the_save_refusal_arithmetic():
    import routes.bot_routes as br
    cfg = {"risk_level": "medium", "adaptive_sizing_enabled": False}
    assert br.preset_cap_fit(cfg, {"min_confidence_override": 70}, None) is None          # no cap → nothing to show
    fit = br.preset_cap_fit(cfg, {"min_confidence_override": 70}, 1.0)
    assert fit == {"effective_risk_pct": 1.0, "cap_pct": 1.0, "fits_cap": True}
    over = br.preset_cap_fit(cfg, {"risk_level": "high"}, 1.0)                           # a style lifting risk to 2.5 %
    assert over["fits_cap"] is False and over["effective_risk_pct"] == 2.5
    # the stored active_preset never leaks into the candidate's verdict
    assert br.preset_cap_fit({**cfg, "active_preset": "fast_scalp"}, {}, 1.0)["fits_cap"] is True


def test_presets_endpoint_annotates_builtins_and_custom_with_cap_fit(monkeypatch):
    import routes.bot_routes as br
    db = FakeDb()
    monkeypatch.setattr(br, "get_db", lambda: db)
    oid = ObjectId()
    _run(db.accounts.insert_one({"_id": oid, "user_id": "u1", "status": "connected"}))
    _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": str(oid), "risk_level": "high",
                                    "adaptive_sizing_enabled": False}))

    async def _custom(user_id):
        return [{"id": "c1", "name": "mine", "config": {"risk_level": "low"}},
                {"id": "c2", "name": "hot", "config": {"risk_level": "extreme"}}]
    monkeypatch.setattr(br, "list_user_presets", _custom)
    _cap(monkeypatch, 1.0)
    out = _run(br.get_strategy_presets(account_id=str(oid), user={"id": "u1"}))
    assert out["policy_risk_cap_pct"] == 1.0
    # built-ins carry no risk knobs → they inherit the account's HIGH level (2.5 %) and are all over the 1 % cap
    assert out["presets"] and all(p["cap_fit"]["fits_cap"] is False and p["cap_fit"]["effective_risk_pct"] == 2.5
                                  for p in out["presets"])
    by = {c["id"]: c["cap_fit"] for c in out["custom"]}
    assert by["c1"]["fits_cap"] is True and by["c1"]["effective_risk_pct"] == 0.5         # custom style lowering risk fits
    assert by["c2"]["fits_cap"] is False and by["c2"]["effective_risk_pct"] == 2.0
    # no active cap → verdicts are null and the cap field is null
    _cap(monkeypatch, None)
    out = _run(br.get_strategy_presets(account_id=str(oid), user={"id": "u1"}))
    assert out["policy_risk_cap_pct"] is None and all(p["cap_fit"] is None for p in out["presets"] + out["custom"])


def test_apply_preset_refuses_over_cap_style_with_the_save_error(monkeypatch):
    from fastapi import HTTPException
    import routes.bot_routes as br
    import strategy_presets as sp
    db = FakeDb()
    monkeypatch.setattr(br, "get_db", lambda: db)
    oid = ObjectId()
    _run(db.accounts.insert_one({"_id": oid, "user_id": "u1", "status": "connected"}))
    _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": str(oid), "risk_level": "low",
                                    "adaptive_sizing_enabled": False, "active": False}))
    fake = lambda k: ({"label": "Hot", "config": {"risk_level": "high"}} if k == "hot"  # noqa: E731
                      else {"label": "Calm", "config": {"min_confidence_override": 70}} if k == "calm" else None)
    monkeypatch.setattr(sp, "get_preset", fake)
    monkeypatch.setattr(br, "get_preset", fake)
    _cap(monkeypatch, 1.0)
    with pytest.raises(HTTPException) as ei:
        _run(br.apply_strategy_preset("hot", account_id=str(oid), user={"id": "u1"}))
    d = ei.value.detail
    assert ei.value.status_code == 409 and d["code"] == "risk_above_policy_cap" and d["effective_pct"] == 2.5
    assert _run(db.bot_configs.find_one({"user_id": "u1"})).get("active_preset") is None   # nothing was overlaid
    out = _run(br.apply_strategy_preset("calm", account_id=str(oid), user={"id": "u1"}))
    assert out["applied"] == "calm" and out["config"]["active_preset"] == "calm"


def test_bot_config_page_renders_cap_fit_before_the_pick():
    src = open(os.path.join(ROOT, "frontend", "src", "pages", "BotConfig.jsx"), encoding="utf-8").read()
    assert "api.get(`/bot/presets${accountQuery}`)" in src                      # fit is per selected account
    assert "setPresetCap(pr.data.policy_risk_cap_pct ?? null)" in src
    for marker in ("presets-policy-cap-banner", "preset-cap-fit-${p.key}", "custom-preset-cap-fit-${p.id}",
                   "FITS CAP ≤", "OVER CAP ·", "BLOCKED BY POLICY CAP — LOWER RISK FIRST",
                   "disabled={isApplying || overCap}"):
        assert marker in src, marker
    assert src.count("disabled={isApplying || overCap}") == 2                   # built-in cards AND custom presets


def test_78_preset_apply_is_versioned_and_audited(monkeypatch):
    """#78 — preset-apply goes through config_promotion.apply_config_change: config_version + pointer + audit entry
    listing the fields the preset changed (previously a bare update_one with no version / audit)."""
    import routes.bot_routes as br
    import strategy_presets as sp
    import config_promotion as cp
    db = FakeDb()
    monkeypatch.setattr(br, "get_db", lambda: db)
    monkeypatch.setattr(cp, "_txn_support", False)
    oid = ObjectId()
    _run(db.accounts.insert_one({"_id": oid, "user_id": "u1", "status": "connected"}))
    _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": str(oid), "risk_level": "low", "strategy_engine": "breakout",
                                    "strategy_mode": "breakout", "max_risk_per_trade_pct": 0.5}))
    fake = lambda key: {"label": "Mean reversion", "config": {"strategy_mode": "mean_reversion", "max_risk_per_trade_pct": 0.5}}  # noqa: E731
    monkeypatch.setattr(sp, "get_preset", fake)
    monkeypatch.setattr(br, "get_preset", fake)
    _cap(monkeypatch, 1.0)
    out = _run(br.apply_strategy_preset("mean_reversion", account_id=str(oid), user={"id": "u1"}))
    assert out["applied"] == "mean_reversion" and out["config_version_id"]
    assert out["preset_fields"] == ["active_preset", "max_risk_per_trade_pct", "strategy_mode"]
    cfg = _run(db.bot_configs.find_one({"user_id": "u1"}))
    assert cfg["strategy_mode"] == "mean_reversion" and cfg["active_preset"] == "mean_reversion"
    assert cfg["field_sources"]["strategy_mode"] == "preset_apply" and "max_risk_per_trade_pct" not in cfg.get("field_sources", {})
    assert len(db.config_versions.rows) == 1 and db.config_versions.rows[0]["label"] == "preset:mean_reversion"
    assert str(db.config_versions.rows[0]["_id"]) == out["config_version_id"]
    assert db.config_pointers.rows and db.promotion_journal.rows[0]["status"] == "complete"
    audit = [a for a in db.audit_log.rows if a["action"] == "config_change_applied"]
    assert len(audit) == 1
    d = audit[0]["detail"]
    assert d["preset_key"] == "mean_reversion" and d["preset_label"] == "Mean reversion" and d["preset_kind"] == "builtin"
    assert d["preset_fields"] == out["preset_fields"]
    assert d["changed"]["strategy_mode"] == {"from": "breakout", "to": "mean_reversion"}      # genuinely changed only
    assert "max_risk_per_trade_pct" not in d["changed"] and d["version_id"] == out["config_version_id"]
