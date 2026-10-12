"""Phase 1 — "remove the traps" (STOIC Simple Onboarding Spec, items U2 U3 U8 U9 U10 U11 U16 U18 O1 O2 O3)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


def _acc(**kw):
    base = {"_id": ObjectId(), "user_id": "u1", "mode": "live", "label": "demo", "account_number": "1083440",
            "server": "OnEquity-Demo", "broker_server": "OnEquity-Demo", "expected_identity": {"broker_server": "OnEquity-Demo"}}
    base.update(kw)
    return base


# ── U2 auto server adopt ──────────────────────────────────────────────────────────────────────────────
def test_u2_adopt_rules():
    import server_adopt as sa
    acc = _acc()
    assert sa.adoption_applies(acc, 1083440, "OnEquity-Server") is True              # same login, different name
    assert sa.adoption_applies(acc, 1083440, "onequity-demo") is False               # alias-equal → nothing to do
    assert sa.adoption_applies(acc, 9999, "OnEquity-Server") is False                # login mismatch NEVER adopts
    assert sa.adoption_applies(acc, None, "OnEquity-Server") is False
    assert sa.adoption_applies(_acc(mode="paper"), 1083440, "X") is False
    assert sa.adoption_applies(_acc(server="", broker_server="", expected_identity={}), 1083440, "OnEquity-Server") is True   # empty → fill
    assert sa.adoption_applies(acc, 1083440, "bad;name") is False


def test_u2_adopt_writes_and_audits(monkeypatch):
    import server_adopt as sa
    audits = []

    async def _audit(db, entry, **k):
        audits.append(entry)
    monkeypatch.setattr(sa, "append_chained", _audit)
    db = FakeDb()
    acc = _acc(verified_identity={"broker_server": "OnEquity-Demo"}, broker_account_mismatch=True)
    _run(db.accounts.insert_one(dict(acc)))
    # H3 — without the identity chain (installation + lease) nothing is adopted
    assert _run(sa.maybe_adopt_reported_server(db, acc, 1083440, "OnEquity-Server", installation_id="inst_0123456789ab")) is None
    _run(db.installations.insert_one({"installation_id": "inst_0123456789ab", "account_id": str(acc["_id"]), "revoked": False}))
    _run(db.execution_leases.insert_one({"account_id": str(acc["_id"]), "installation_id": "inst_0123456789ab"}))
    ev = _run(sa.maybe_adopt_reported_server(db, acc, 1083440, "OnEquity-Server", installation_id="inst_0123456789ab"))
    assert ev["to"] == "OnEquity-Server" and ev["from"] == "OnEquity-Demo" and ev["token_authenticated"] is True
    row = _run(db.accounts.find_one({"_id": acc["_id"]}))
    assert row["server"] == row["broker_server"] == row["expected_identity"]["broker_server"] == "OnEquity-Server"
    assert row["broker_account_mismatch"] is False and "verified_identity" not in row
    assert row["server_changed"]["source"] == "ea_auto_adopt"
    assert audits[0]["action"] == "server_adopted_from_ea" and audits[0]["meta"]["mt5_login"] == "1083440"
    assert acc["server"] == "OnEquity-Server"                                        # caller's doc updated in place
    assert _run(sa.maybe_adopt_reported_server(db, acc, 1083440, "OnEquity-Server", installation_id="inst_0123456789ab")) is None   # idempotent
    # H3 — a second terminal reporting the OLD name within 6 h is a flip-flop: finding, no adoption
    findings = []

    async def _open(db_, finding):
        findings.append(finding)
    monkeypatch.setattr("security_agent.findings.open_or_update", _open)
    assert _run(sa.maybe_adopt_reported_server(db, acc, 1083440, "OnEquity-Demo", installation_id="inst_0123456789ab")) is None
    assert findings and findings[0]["check_id"] == "server_flipflop" and acc["server"] == "OnEquity-Server"
    # M5 — a manual edit is locked against auto-adopt for 24 h
    acc["server_changed"] = {"from": "x", "to": "OnEquity-Server", "at": sa.datetime.now(sa.timezone.utc).isoformat(), "source": "owner_edit", "by": "u1"}
    assert sa.manual_lock_active(acc) is True
    assert _run(sa.maybe_adopt_reported_server(db, acc, 1083440, "OnEquity-Live", installation_id="inst_0123456789ab")) is None
    acc["server_changed"]["at"] = "2020-01-01T00:00:00+00:00"
    assert sa.manual_lock_active(acc) is False


def test_u2_heartbeat_adopts_only_after_identity_chain_with_full_token():
    """H3 (main127) — adoption runs AFTER verify_heartbeat_identity, only for a server-name-only failure, only
    with a FULL (current) token, and never changes this heartbeat's identity result."""
    src = _read("backend", "routes", "bridge_routes.py")
    hb = src.split('@router.post("/heartbeat")')[1].split("@router.")[0]
    assert "maybe_adopt_reported_server(db, acc, payload.account_login, payload.broker_server" in hb
    assert hb.index("verify_heartbeat_identity(") < hb.index("maybe_adopt_reported_server")
    assert '"broker server mismatch" in str(identity.get("reason") or "") and acc.get("_token_kind") == "current"' in hb
    assert "require_identity=True" in hb
    adopt_block = hb.split("maybe_adopt_reported_server")[1][:600]
    assert "identity[\"ok\"] = True" not in adopt_block and "mismatch = False" not in adopt_block
    assert 'acc["_token_kind"] = "current"' in src and 'acc["_token_kind"] = "grace"' in src


# ── U3 Edit: server admin-only ─────────────────────────────────────────────────────────────────────────
def test_u3_server_edit_admin_only():
    src = _read("backend", "routes", "account_routes.py")
    patch = src.split('@router.patch("/{account_id}")')[1].split("@router.")[0]
    assert 'if not is_admin:' in patch and '"admin_only"' in patch
    # M6 (main127) — admin check comes FIRST: admins reach any account, users only their own
    assert 'acc = await db.accounts.find_one({"_id": oid} if is_admin else {"_id": oid, "user_id": user["id"]})' in patch
    assert patch.index('is_admin = user.get("role") == "admin"') < patch.index("db.accounts.find_one(")
    dlg = _read("frontend", "src", "components", "EditAccountDialog.jsx")
    assert 'const isAdmin = user?.role === "admin";' in dlg and 'data-testid="edit-account-server-auto"' in dlg
    assert 'const isLive = account && account.mode !== "paper" && isAdmin;' in dlg


# ── U8 / U18 EA ─────────────────────────────────────────────────────────────────────────────────────────
def test_u8_u18_ea_1_64():
    mq5 = _read("backend", "static", "EmergentTradingBridge.mq5")
    assert '#define EA_CLIENT_VERSION "1.64"' in mq5 and '#property version   "1.64"' in mq5
    assert "bool IsLegacyDefaultUrl(string u)" in mq5
    for legacy in ('"https://www.stoicaibot.com"', '"https://your-app.preview.emergentagent.com"', '".preview.emergentagent.com"'):
        assert legacy in mq5.split("bool IsLegacyDefaultUrl")[1].split("\n}\n")[0], legacy
    srv = mq5.split("string ResolveServerUrl()")[1].split("int OnInit()")[0]
    assert "if (StringLen(input_trim) > 0 && !IsLegacyDefaultUrl(input_trim))" in srv
    assert 'input_trim != SERVER_URL_DEFAULT) {' not in srv
    post = mq5.split("string HttpPost(string url, string body)")[1].split("\n}\n")[0]
    assert "if (g_account_deleted) return \"\";" in post
    assert 'if (res == 410 && StringFind(answer, "account_deleted") >= 0)' in post and "ExpertRemove();" in post
    # heartbeat JSON still carries login / server / margin / trade mode
    assert 'margin_mode\\":\\"%s\\",\\"trade_mode\\":\\"%s\\"' in mq5 and 'account_login\\":%I64d' in mq5


# ── U9 auto DEMO attestation ───────────────────────────────────────────────────────────────────────────
def _demo_acc(**kw):
    now = datetime.now(timezone.utc)
    acc = _acc(server="OnEquity-Server", broker_server="OnEquity-Server", expected_identity={"broker_server": "OnEquity-Server"},
               account_type="demo", broker_environment="DEMO", broker_account_id_reported="1083440",
               last_heartbeat=now.isoformat(), account_trade_mode="demo", account_trade_mode_reported_at=now.isoformat(),
               ea_identity={"installation_id": "inst_0123456789ab", "authoritative": True, "broker_server": "OnEquity-Server",
                            "trade_mode": "demo"},
               ea_binary_sha256="a" * 64, ea_binary_sha256_method="installer_attested")
    acc.update(kw)
    return acc


def test_u9_grant_requires_every_check(monkeypatch):
    import auto_attest as aa
    import broker_env
    monkeypatch.setattr(broker_env, "ea_binary_accepted", lambda a: str(a.get("ea_binary_sha256_method") or "") == "installer_attested"
                        and str(a.get("ea_binary_sha256") or "") == "a" * 64)
    ok = aa.evaluate(_demo_acc(), accepted_hashes=["A" * 64])
    assert ok["grant"] is True and all(ok["checks"].values())
    assert aa.evaluate(_demo_acc(), accepted_hashes=["b" * 64])["checks"]["ea_hash_signed"] is False       # unsigned binary
    echo = aa.evaluate(_demo_acc(ea_binary_sha256_method="unattested"), accepted_hashes=["a" * 64])        # heartbeat echo is not evidence
    assert echo["grant"] is False and echo["checks"]["ea_hash_signed"] is False
    assert aa.evaluate(_demo_acc(ea_binary_sha256=None), accepted_hashes=["a" * 64])["grant"] is False
    nomode = _demo_acc(account_trade_mode=None); nomode["ea_identity"]["trade_mode"] = None
    assert aa.evaluate(nomode, accepted_hashes=["a" * 64])["grant"] is False                                 # no broker word → fail closed
    assert aa.evaluate(_demo_acc(ea_identity={"installation_id": "x", "authoritative": False}), accepted_hashes=["a" * 64])["grant"] is False
    assert aa.evaluate(_demo_acc(account_type="live", broker_environment="LIVE"), accepted_hashes=["a" * 64])["grant"] is False
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert aa.evaluate(_demo_acc(last_heartbeat=stale), accepted_hashes=["a" * 64])["grant"] is False       # stale heartbeat
    # manual attestation present → never overwritten
    man = _demo_acc(environment_attestation={"approved_by": "admin@x", "environment": "DEMO"})
    assert aa.evaluate(man, accepted_hashes=["a" * 64])["grant"] is False
    # broker says real → void (manual or automatic)
    real = _demo_acc(account_trade_mode="real", environment_attestation={"approved_by": "admin@x"}); real["ea_identity"]["trade_mode"] = "real"
    v = aa.evaluate(real, accepted_hashes=["a" * 64])
    assert v["void"] is True and v["grant"] is False


def test_u9_apply_grants_then_voids(monkeypatch):
    import auto_attest as aa
    import broker_env
    from broker_env import attested_environment, attestation_state
    monkeypatch.setattr(broker_env, "ea_binary_accepted", lambda a: True)
    audits = []

    async def _audit(db, entry, **k):
        audits.append(entry)
    monkeypatch.setattr(aa, "append_chained", _audit)
    db = FakeDb()
    acc = _demo_acc()
    _run(db.accounts.insert_one(dict(acc)))
    assert _run(aa.apply(db, acc, accepted_hashes=["a" * 64])) == "granted"
    row = _run(db.accounts.find_one({"_id": acc["_id"]}))
    att = row["environment_attestation"]
    assert att["approved_by"] == aa.AUTO_APPROVER and att["proof"]["verifier"] == aa.AUTO_VERIFIER
    assert attested_environment(row) == "DEMO" and attestation_state(row) == "valid"          # the card shows DEMO verified
    assert audits[-1]["action"] == "account_environment_attest" and audits[-1]["meta"]["verifier"] == aa.AUTO_VERIFIER
    assert _run(aa.apply(db, row, accepted_hashes=["a" * 64])) is None                        # idempotent
    row["account_trade_mode"] = "real"; row["ea_identity"]["trade_mode"] = "real"
    assert _run(aa.apply(db, row, accepted_hashes=["a" * 64])) == "voided"
    assert "environment_attestation" not in _run(db.accounts.find_one({"_id": acc["_id"]}))
    assert audits[-1]["action"] == "account_environment_attestation_voided"
    hb = _read("backend", "routes", "bridge_routes.py").split('@router.post("/heartbeat")')[1]
    assert "await auto_attest.apply(db, _fresh)" in hb


# ── U10 margin mode from the EA ─────────────────────────────────────────────────────────────────────────
def test_u10_ea_margin_mode_beats_admin_setting():
    from routes.bridge_routes import position_mode_resolution
    acc = _acc(margin_mode="netting", margin_mode_reported_at="2026-10-11T00:00:00+00:00",
               position_mode_override={"mode": "hedging", "by": "admin@x"})
    r = _run(position_mode_resolution(None, acc))
    assert r["mode"] == "netting" and r["source"] == "ea" and r["admin_override_contradicted"]["mode"] == "hedging"
    r2 = _run(position_mode_resolution(None, _acc(position_mode_override={"mode": "netting"})))
    assert r2 == {"mode": "netting", "source": "admin", "by": None, "at": None}


# ── U11 one bot per account ─────────────────────────────────────────────────────────────────────────────
def test_u11_no_account_less_configs_and_orphan_migration():
    import bot_inventory as bi
    from routes.bot_routes import _get_or_create_config, _resolve_account_scope
    db = FakeDb()
    # no account → virtual defaults, nothing persisted
    cfg = _run(_get_or_create_config(db, "u1", None))
    assert cfg["virtual"] is True and cfg["_id"] is None and _run(db.bot_configs.count_documents({})) == 0
    aid = ObjectId()
    _run(db.accounts.insert_one({"_id": aid, "user_id": "u1", "status": "connected"}))
    bound = _run(bi.ensure_account_bot(db, "u1", str(aid)))
    assert bound["account_id"] == str(aid) and bound["active"] is False
    assert _run(db.bot_configs.count_documents({"user_id": "u1"})) == 1
    assert _run(_resolve_account_scope(db, "u1", None)) == str(aid)               # default scope → the only account
    assert _run(_get_or_create_config(db, "u1", None))["_id"] == bound["_id"]       # no second config
    # M4 (main127) migration: INACTIVE orphans are quarantined (restorable), an ACTIVE orphan is never deleted
    _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": None, "active": False}))
    _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": str(ObjectId()), "active": True}))
    _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": str(ObjectId()), "active": False}))
    out = _run(bi.delete_orphan_bot_configs(db, reason="test"))
    assert out["quarantined_null_account"] == 1 and out["quarantined_unknown_account"] == 1 and out["active_orphans_kept"] == 1
    assert _run(db.bot_configs.count_documents({})) == 2                            # bound one + the ACTIVE orphan
    assert _run(db.bot_configs_orphaned.count_documents({})) == 2
    assert all(r["orphaned_reason"] == "test" for r in db.bot_configs_orphaned.rows)
    assert "delete_many" not in _read("backend", "bot_inventory.py").split("if c.get(\"active\"):")[0]   # no delete before the active check
    seed = _read("backend", "seed.py")
    assert "delete_orphan_bot_configs(db, reason=\"startup-migration\")" in seed and '"account_id": None,\n            "risk_level"' not in seed
    auth = _read("backend", "routes", "auth_routes.py")
    assert "U11 (Phase 1) — no account-less default bot profile at signup" in auth
    acc_src = _read("backend", "routes", "account_routes.py")
    assert "await ensure_account_bot(db, user[\"id\"], str(doc[\"_id\"]))" in acc_src
    assert 'await db.bot_configs.delete_many({"account_id": account_id})' in acc_src


# ── U16 diagnostics ─────────────────────────────────────────────────────────────────────────────────────
def test_u16_webrequest_blocked_from_ea_self_check():
    import install_progress as ip
    now = datetime.now(timezone.utc)
    acc = {"_id": "a", "account_number": "1", "installer_paired_at": (now - timedelta(minutes=5)).isoformat(), "installer_version": "1.8",
           "last_heartbeat": now.isoformat(), "ea_self_check": {"webrequest_ok": False, "autotrading": True, "at": now.isoformat()}}
    out = ip.derive(acc, None, None, attested=(None, None), accepted_hashes=[], now=now)
    hb = next(s for s in out["steps"] if s["id"] == "heartbeat")
    assert hb["status"] == "warn" and "MT5 blocks the address" in hb["hint"] and "4014" in hb["detail"]
    src = _read("backend", "install_progress.py")
    assert "Most likely the WebRequest URL is missing" not in src
    assert "No request from this terminal has reached the server — MT5 blocks the address or the EA is not on a chart" in src


# ── U18 tombstones ─────────────────────────────────────────────────────────────────────────────────────
def test_u18_tombstone_and_410():
    import account_tombstones as at
    db = FakeDb()
    acc = _acc(bridge_token_hash="h1", bridge_token_prev_hash="h0", bridge_token_retired_hashes=["hx"])
    tomb = _run(at.write_tombstone(db, acc, deleted_by="u1"))
    assert tomb["token_hashes"] == ["h0", "h1", "hx"] and "bridge_token" not in str(tomb)
    assert _run(at.find_by_token_hash(db, "hx"))["_id"] == str(acc["_id"]) and _run(at.find_by_token_hash(db, "zz")) is None
    assert _run(at.note_poll(db, tomb, ip="1.2.3.4")) is True                         # first poll → one finding
    assert _run(at.note_poll(db, tomb, ip="1.2.3.4")) is False
    row = _run(db.account_tombstones.find_one({"_id": tomb["_id"]}))
    assert row["polls_after_delete"] == 2 and row["finding_recorded"] is True
    src = _read("backend", "routes", "bridge_routes.py")
    gate = src.split("async def _account_by_token")[1].split("\n\n\n")[0]
    assert "find_by_token_hash(db, bt.token_hash(token))" in gate and "status_code=410" in gate and '"account_deleted"' in gate
    assert '"deleted_account_poll"' in gate and '"low"' in gate                       # lowest severity the agent has (no "info" level)
    from security_agent.checks.common import f as _f
    assert _f("deleted_account_poll", "account:x", "low", "bridge", "x", {})["title"] == "EA of a deleted account still polling"
    assert _f("proxy_misconfiguration", "ip:x", "high", "platform", "x", {})["severity"] == "high"
    assert gate.index("status_code=410") < gate.index("retired = await bt.is_retired")      # tombstone checked before "unknown"
    b2 = _read("backend", "security_agent", "checks", "bridge_secrets_deps.py")
    assert '(e.get("detail") or {}).get("deleted_account")' in b2 and "iso_ago(minutes=10)" in b2


# ── O1 security agent ───────────────────────────────────────────────────────────────────────────────────
def test_o1_cloudflare_ip_is_reclassified_not_blocked():
    from security_agent import rules
    cfg = {"ip_block_min": 60, "thresholds": {}}
    cf = {"check_id": "B2", "dedup_key": "B2:ip:104.16.0.1", "evidence": {"ip": "104.16.0.1", "requests": 500}}
    props = rules.evaluate(cf, cfg)
    assert len(props) == 1 and props[0]["action"] == "none" and props[0]["blocked_by"] == "cloudflare_edge"
    assert props[0]["reclassified"] == "proxy_misconfiguration" and "TRUST_CF_CONNECTING_IP" in props[0]["note"]
    assert "severity_escalation" not in props[0]
    normal = {"check_id": "B2", "dedup_key": "B2:ip:203.0.113.9", "evidence": {"ip": "203.0.113.9", "requests": 500}}
    assert rules.evaluate(normal, cfg)[0]["action"] == "block_ip"
    act = _read("backend", "security_agent", "actions.py")
    assert 'if prop.get("blocked_by") == "cloudflare_edge":' in act and '"proxy_misconfiguration"' in act and '"high"' in act


# ── O2 / O3 deploy scripts ──────────────────────────────────────────────────────────────────────────────
def test_o2_cloudflare_client_ip_checks():
    assert 'set_kv backend/.env TRUST_CF_CONNECTING_IP true' in _read("deploy", "install.sh")
    doc = _read("deploy", "doctor.sh")
    assert 'fail "cloudflare overlay active but TRUST_CF_CONNECTING_IP is not true' in doc
    assert 'CLOUDFLARE_MODE: "true"' in _read("docker-compose.cloudflare.yml")
    import demo_readiness as dr
    src = _read("backend", "demo_readiness.py")
    assert 'cf_status = "fail" if (cf_overlay and not cf) else ("pass" if cf_overlay else "info")' in src
    assert dr is not None


def test_o3_release_precheck_before_backup():
    up = _read("deploy", "update.sh")
    assert "python3 scripts/release_precheck.py --repo" in up
    assert up.index("release_precheck.py") < up.index('echo "-- pre-update backup"')
    assert 'nothing was changed on this host (no backup, no checkout)' in up
    assert '[ "${STOIC_ALLOW_UNATTESTED:-0}" != 1 ]' in up
    import importlib.util
    spec = importlib.util.spec_from_file_location("release_precheck", os.path.join(ROOT, "scripts", "release_precheck.py"))
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    rp.tag_on_origin = lambda tag: tag == "v9.9.9"
    assert rp.main(["--repo", "o/r", "--tag", "v0.0.0"]) == 2
    rp.release_run = lambda repo, tag, token: {"found": True, "status": "in_progress", "conclusion": None, "url": "u"}
    assert rp.main(["--repo", "o/r", "--tag", "v9.9.9"]) == 3
    rp.release_run = lambda repo, tag, token: {"found": True, "status": "completed", "conclusion": "failure", "url": "u"}
    assert rp.main(["--repo", "o/r", "--tag", "v9.9.9"]) == 3
    rp.release_run = lambda repo, tag, token: {"found": False}
    assert rp.main(["--repo", "o/r", "--tag", "v9.9.9"]) == 3
    rp.release_run = lambda repo, tag, token: {"found": True, "status": "completed", "conclusion": "success", "url": "u"}
    assert rp.main(["--repo", "o/r", "--tag", "v9.9.9"]) == 0

    def _boom(repo, tag, token):
        raise rp.urllib.error.URLError("offline")
    rp.release_run = _boom
    assert rp.main(["--repo", "o/r", "--tag", "v9.9.9"]) == 4
    assert "from __future__ import annotations" in _read("scripts", "release_precheck.py")
