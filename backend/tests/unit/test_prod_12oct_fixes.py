"""Production issues found on stoicaibot.com 12 Oct (v1.60.21): (1) rules-policy upload 422 for whole-number float
risk caps + "repo not configured"; (2) nginx stale upstream after restart.sh --env-changed; (3) ea_release gate without
the key pins in the host shell + ATTESTATION_REQUIRED=false written by bootstrap on production hosts."""
import asyncio
import base64
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
KEY = Ed25519PrivateKey.generate()
PUB_B64 = base64.b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


@pytest.fixture
def signed_text(monkeypatch):
    """A policy file EXACTLY as the signer wrote it — with `5.0` whole-number floats in risk_caps."""
    import release_signing as rs
    import rules_policy as rp
    from release_signing import domain_bytes
    real_verify = rs.verify_hex
    monkeypatch.setattr(rs, "verify_hex", lambda data, sig, pub_b64=None, *, purpose: real_verify(data, sig, PUB_B64, purpose=purpose))
    monkeypatch.setenv("APP_ENV", "preview")
    monkeypatch.delenv("STOIC_INSTALLATION_ID", raising=False)

    async def _audit(db, entry, **k):
        pass
    import audit_chain
    monkeypatch.setattr(audit_chain, "append_chained", _audit)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pol = {"schema": rp.SCHEMA, "installation_id": "dev-local", "environment": "preview", "policy_version": "demo-rules-v1",
           "demo_only": True, "max_accounts_per_user": 1, "allowed_brokers": ["*"],
           "risk_caps": {"max_daily_loss_pct": 5.0, "max_risk_pct": 1.0},
           "reason": "prod 17:47:34 UTC repro", "issuer": "ops@x", "issued_at": now.isoformat(),
           "expires_at": (now + timedelta(days=30)).isoformat(), "nonce": secrets.token_hex(8)}
    pol["signature_hex"] = KEY.sign(domain_bytes(rp.PURPOSE, rp.body(pol))).hex()
    text = json.dumps(pol, indent=2)                       # Python keeps 5.0 → the file literally contains "5.0"
    assert '"max_daily_loss_pct": 5.0' in text
    return text


# ── 1 · rules policy upload ───────────────────────────────────────────────────────────────────────────
def test_1_browser_json_parse_loses_the_float_and_breaks_the_signature(signed_text):
    import rules_policy as rp
    original = json.loads(signed_text)
    assert rp.problems(original) == []                           # the file itself is fine (as on prod)
    browser_dict = json.loads(signed_text, parse_float=lambda s: int(float(s)) if float(s).is_integer() else float(s))   # JSON.parse: 5.0 → 5
    assert browser_dict["risk_caps"]["max_daily_loss_pct"] == 5 and isinstance(browser_dict["risk_caps"]["max_daily_loss_pct"], int)
    assert rp.body(browser_dict) != rp.body(original)
    assert any("signature" in e for e in rp.problems(browser_dict))   # exactly the prod 422


def test_1_upload_route_accepts_the_raw_text_and_activates(signed_text, monkeypatch):
    import routes.authority_routes as ar
    db = FakeDb()
    monkeypatch.setattr(ar, "get_db", lambda: db)

    async def _step(db_, user, request, action):
        pass

    async def _audit_event(db_, uid, action, meta, request):
        pass
    monkeypatch.setattr("step_up.require_step_up", _step)
    monkeypatch.setattr("step_up.audit_event", _audit_event)
    user = {"id": "adm", "email": "admin@x", "role": "admin"}
    out = _run(ar.rules_policy_upload({"policy_text": signed_text}, None, user=user))
    assert out["ok"] and out["policy_version"] == "demo-rules-v1"
    doc = _run(db.platform_state.find_one({"_id": "rules_policy"}))
    assert doc["risk_caps"]["max_daily_loss_pct"] == 5.0
    # bad text → 422 policy_not_json, a JSON list → 422 policy_not_object, oversize → 413
    for payload, code, status in (({"policy_text": "{not json"}, "policy_not_json", 422), ({"policy_text": "[1,2]"}, "policy_not_object", 422),
                                  ({"policy_text": "x" * 70000}, "policy_too_large", 413)):
        with pytest.raises(HTTPException) as ei:
            _run(ar.rules_policy_upload(payload, None, user=user))
        assert ei.value.status_code == status and ei.value.detail["code"] == code
    fe = _read("frontend", "src", "components", "admin", "RulesPolicyPanel.jsx")
    assert 'api.post("/authority/policy/upload", { policy_text: text })' in fe and 'api.post("/authority/policy/upload", { policy })' not in fe


def test_1_load_latest_reports_repo_not_configured(monkeypatch):
    import rules_policy as rp
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.setattr(rp, "local_candidates", lambda: [])
    out = _run(rp.load_latest(FakeDb(), actor="t"))
    assert out["activated"] is False and out["reason"] == "repo not configured" and out["repo_configured"] is False
    assert "GITHUB_REPO" in out["note"]
    monkeypatch.setenv("GITHUB_REPO", "stoic/bot")

    async def _none():
        return []
    monkeypatch.setattr(rp, "fetch_candidates_from_github", _none)
    out2 = _run(rp.load_latest(FakeDb(), actor="t"))
    assert out2["reason"] == "no valid signed policy found" and out2["repo_configured"] is True and out2["note"] is None
    fe = _read("frontend", "src", "components", "admin", "RulesPolicyPanel.jsx")
    assert 'data-testid="rules-policy-repo-not-configured"' in fe and "repo not configured" in fe
    assert '"repo_configured": bool(rp.github_repo())' in _read("backend", "routes", "authority_routes.py")


# ── 2 · nginx re-resolution + health through the proxy ────────────────────────────────────────────────
def test_2_nginx_re_resolves_the_backend_and_restart_waits_through_the_frontend():
    conf = _read("deploy", "nginx.conf")
    assert "resolver 127.0.0.11 valid=10s" in conf and "set $backend http://backend:8001;" in conf and "proxy_pass $backend;" in conf
    assert "proxy_pass http://backend:8001;" not in conf
    assert conf.index("set $backend") < conf.index("location /api/") < conf.index("proxy_pass $backend;")
    api_block = conf.split("location /api/ {")[1].split("location / {")[0]
    for h in ("proxy_set_header Host $host;", "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;", 'proxy_set_header Connection "upgrade";', "proxy_read_timeout 300s;"):
        assert h in api_block, h
    lib = _read("deploy", "lib.sh")
    assert "wait_api_health_via_frontend()" in lib and "http://127.0.0.1:3000/api/health" in lib and '[ "${code}" = "200" ] && return 0' in lib
    rs = _read("deploy", "restart.sh")
    assert "wait_api_health_via_frontend 45 ||" in rs and rs.index("wait_api_health 30") < rs.index("wait_api_health_via_frontend 45") < rs.index("restart complete")


# ── 3 · ea_release gate in an empty host shell ────────────────────────────────────────────────────────
def test_3_verify_ea_release_reads_the_three_pins_from_backend_env(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import verify_ea_release as v
    envf = tmp_path / "backend.env"
    envf.write_text('RELEASE_PUBLIC_KEY_B64="pub=="\nRELEASE_SIGNER_KEY_ID=stoic-release-ed25519-v2\nRELEASE_SIGNER=external\n'
                    'RELEASE_SIGNER_TOKEN=must-not-load\nMONGO_URL=mongodb://x\n# RELEASE_SIGNER=commented\n')
    env = {}
    loaded = v.load_backend_env_pins(str(envf), env)
    assert loaded == {"RELEASE_PUBLIC_KEY_B64": "pub==", "RELEASE_SIGNER_KEY_ID": "stoic-release-ed25519-v2", "RELEASE_SIGNER": "external"}
    assert set(env) == set(v.PIN_KEYS)                           # nothing else leaks into the process env
    env2 = {"RELEASE_SIGNER": "local"}
    v.load_backend_env_pins(str(envf), env2)
    assert env2["RELEASE_SIGNER"] == "local"                      # an exported value wins
    assert v.load_backend_env_pins(str(tmp_path / "missing"), {}) == {}
    assert "load_backend_env_pins()" in _read("scripts", "verify_ea_release.py").split("def check(args):")[1]


def test_3_gate_runs_in_an_empty_shell_environment(tmp_path):
    """`env -i python3 scripts/verify_ea_release.py --check` with ONLY the backend/.env file available — the pins
    must come from the file. With the real v2 public key the key resolution succeeds (fingerprint pinned in
    release/release_key.fingerprint); the outcome then depends solely on the EA record in this checkout."""
    manifest = json.load(open(os.path.join(ROOT, "backend", "models_store", "MODEL_MANIFEST.json")))
    envf = tmp_path / "backend.env"
    envf.write_text(f"RELEASE_PUBLIC_KEY_B64={manifest['public_key_b64']}\nRELEASE_SIGNER_KEY_ID={manifest['key_id']}\nRELEASE_SIGNER=external\n")
    r = subprocess.run(["env", "-i", f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}", f"STOIC_BACKEND_ENV_FILE={envf}",
                        sys.executable, os.path.join(ROOT, "scripts", "verify_ea_release.py"), "--check"], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert "no release public key" not in out and "revoked-or-unknown" not in out and "does NOT verify" not in out, out
    ea = (json.load(open(os.path.join(ROOT, "docs", "RELEASE_HASHES.json"))).get("ea") or {})
    if ea.get("ex5_sha256") and (ea.get("signature") or {}).get("key_id") == manifest["key_id"]:
        assert r.returncode == 0 and "signature verified" in out, out       # a tagged checkout: the record verifies
    # and WITHOUT the file nothing is resolved — the gate stays fail-closed, never a traceback
    r2 = subprocess.run(["env", "-i", f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}", f"STOIC_BACKEND_ENV_FILE={tmp_path / 'none'}",
                         sys.executable, os.path.join(ROOT, "scripts", "verify_ea_release.py"), "--check"], capture_output=True, text=True, timeout=60)
    assert r2.returncode != 0 and "Traceback" not in (r2.stdout + r2.stderr)


def test_3_bootstrap_never_writes_attestation_required_false_on_public_hosts():
    bs = _read("deploy", "bootstrap.sh").split('if [ "${SKIP_ATTEST}" = 1 ]; then')[1].split("# ------------------------------------------------------------------ 4")[0]
    pub, dev = bs.split("else")[0], bs.split("else")[1]
    assert 'if [ "${PUBLIC}" = 1 ]; then' in pub and "export ATTESTATION_REQUIRED=false" in pub and "sed -i '/^ATTESTATION_REQUIRED=/d' .env" in pub
    assert "ATTESTATION_REQUIRED=false' >> .env" not in pub and "ATTESTATION_REQUIRED=false/' .env" not in pub
    assert "echo 'ATTESTATION_REQUIRED=false' >> .env" in dev                    # --dev keeps the old behaviour
    lib = _read("deploy", "lib.sh")
    assert 'v="${ATTESTATION_REQUIRED:-}"' in lib                                   # the exported value is honoured for the bootstrap run


# ── Security audit #23 P3 fixes ───────────────────────────────────────────────────────────────────────
def test_sec023_generic_json_error_and_explicit_edge_body_cap():
    src = _read("backend", "routes", "authority_routes.py").split("async def rules_policy_upload")[1]
    assert 'f"Not valid JSON: {e}"' not in src and '"code": "policy_not_json"' in src
    conf = _read("deploy", "nginx.conf").split("location /api/ {")[1].split("location / {")[0]
    assert "client_max_body_size 1m;" in conf
    assert os.path.exists(os.path.join(ROOT, "docs", "SECURITY_AUDIT_23.md"))
