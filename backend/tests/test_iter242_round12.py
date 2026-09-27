"""Audit round 12 — regression gates.

P1-01 candidate/production separation + atomic promotion · P1-02 provenance
consistency gate · P1-03 archive-context model gate · P1-04 build binding +
dual authenticated approvals + complete provenance · P2-01 summary --check
field-level · P2-02 persisted canonical snapshot + invalidation · P2-03 typed
Turnstile claims · P2-04 break-glass error classification · P2-05 two-admin
inventory-hash approval · P2-06 complete, replay-proof policy migration ·
P2-07 handoff pointer.
"""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_TESTS_DIR)
ROOT = os.path.dirname(_BACKEND_DIR)
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _db():
    from motor.motor_asyncio import AsyncIOMotorClient
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


def _prov(mm, extra=None):
    return {"feature_schema": mm.FEATURE_SCHEMA_VERSION, "feature_code_digest": mm.feature_code_digest(),
            "training_window": {"from": "2026-01-01T00:00:00+00:00", "until": "2026-02-01T00:00:00+00:00"},
            "dataset_sha256": "d" * 64, "n_samples": 120, "trained_at": "2026-02-01T00:00:00+00:00",
            "metrics": {"aucs": {"xgboost": 0.61}, "weights": {"xgboost": 1.0}, "holdout_auc": 0.61},
            "code_commit": mm.running_build_sha(), **(extra or {})}


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    import model_manifest as mm
    import ml_ensemble as me
    monkeypatch.setattr(mm, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(mm, "MANIFEST", tmp_path / "MODEL_MANIFEST.json")
    monkeypatch.setattr(mm, "QUARANTINE", tmp_path / "_quarantine")
    monkeypatch.setattr(me, "MODEL_DIR", tmp_path)
    me._models_cache.clear()
    return tmp_path


class TestModelGovernance:
    def test_manifest_requires_complete_provenance_and_dual_authenticated_approvals(self, model_dir):
        import model_manifest as mm
        (model_dir / "u1").mkdir()
        f = model_dir / "u1" / mm.PRODUCTION_NAME
        f.write_bytes(b"\x80\x04model")
        ok = [{"email": "a@x", "audit_event_id": "e" * 64, "principal_id": "p" * 24},
              {"email": "b@y", "audit_event_id": "f" * 64, "principal_id": "q" * 24}]
        # empty metrics / incomplete window / missing dataset hash → refused
        mm.sidecar_path("u1", "production").write_text(json.dumps(_prov(mm, {"metrics": {}})))
        mm.sign(ok)
        with pytest.raises(mm.ModelRefused, match="required metrics"):
            mm.verify_model(f)
        mm.sidecar_path("u1", "production").write_text(json.dumps(_prov(mm, {"training_window": {"until": "x"}})))
        mm.sign(ok)
        with pytest.raises(mm.ModelRefused, match="training window"):
            mm.verify_model(f)
        mm.sidecar_path("u1", "production").write_text(json.dumps(_prov(mm)))
        # one approver / repeated approver / missing event id → never signable, never loadable
        for bad in ([ok[0]], [ok[0], {**ok[0]}], [ok[0], {"email": "b@y", "audit_event_id": "", "principal_id": "q" * 24}],
                    [ok[0], {**ok[1], "principal_id": ok[0]["principal_id"]}]):        # two emails, ONE principal
            with pytest.raises(mm.ModelRefused):
                mm.sign(bad)
        mm.sign(ok)
        assert mm.verify_model(f) == mm.sha256_file(f)
        # unknown approval event → refused; wrong build → refused; same build → loads
        with pytest.raises(mm.ModelRefused, match="approval audit event unknown"):
            mm.verify_model(f, known_events={"e" * 64})
        assert mm.verify_model(f, known_events={"e" * 64, "f" * 64}) == mm.sha256_file(f)
        with pytest.raises(mm.ModelRefused, match="running build"):
            mm.verify_model(f, build="0" * 40)
        assert mm.verify_model(f, build=mm.running_build_sha())
        # candidate binaries are never loadable even if listed
        cand = model_dir / "u1" / mm.CANDIDATE_NAME
        cand.write_bytes(b"\x80\x04cand")
        with pytest.raises(mm.ModelRefused, match="not listed"):
            mm.verify_model(cand)
        # feature-code drift → refused
        doc = json.loads(mm.MANIFEST.read_text())
        assert doc["body"]["models"][0]["feature_code_digest"] == mm.feature_code_digest()
        mm.sidecar_path("u1", "production").write_text(json.dumps(_prov(mm, {"feature_code_digest": "0" * 64})))
        mm.sign(ok)
        with pytest.raises(mm.ModelRefused, match="feature-code digest"):
            mm.verify_model(f)
        # v1 manifests (approvers: [str]) are refused outright
        doc["body"]["version"] = 1
        mm.MANIFEST.write_text(json.dumps(doc))
        with pytest.raises(mm.ModelRefused):
            mm.load_manifest()

    def test_training_never_touches_production_and_promotion_is_atomic(self, model_dir, monkeypatch):
        """Round 12 P1-01 + round 13 P1-01/P1-03/P2-02: shared content-addressed store, DB pointer
        activation, exact-tuple two-principal approvals, outbox audit, previous production preserved."""
        import model_manifest as mm
        import ml_ensemble as me
        import model_store as ms
        from bson import ObjectId
        db = _db()
        uid = "r12-" + uuid.uuid4().hex[:8]
        p1, p2 = ObjectId(), ObjectId()
        _run(db.users.insert_many([{"_id": p1, "email": f"a-{uid}@x", "role": "admin"}, {"_id": p2, "email": f"b-{uid}@x", "role": "admin"}]))
        try:
            import io
            import joblib

            def _blob(tag):
                buf = io.BytesIO()
                joblib.dump({"tag": tag, "uid": uid}, buf)
                return buf.getvalue()
            prod_bytes, cand_bytes = _blob("prod-v1"), _blob("cand-v2")
            pd, cd = hashlib.sha256(prod_bytes).hexdigest(), hashlib.sha256(cand_bytes).hexdigest()
            _run(ms.put(db, prod_bytes, pd, uid, _prov(mm, {"sha256": pd, "bytes": len(prod_bytes)})))
            _run(ms.put(db, cand_bytes, cd, uid, _prov(mm, {"sha256": cd, "bytes": len(cand_bytes)})))
            _run(db.ml_ensembles.insert_one({"user_id": uid,
                                             "production": {"status": "active", "digest": pd, "weights": {"xgboost": 1.0},
                                                            "aucs": {"xgboost": 0.6}, "n_trades": 100},
                                             "candidate": {"status": "awaiting_approval", "digest": cd, "weights": {"xgboost": 1.0},
                                                           "aucs": {"xgboost": 0.7}, "n_trades": 140, "approvals": []}}))

            def prod_digest():
                return _run(db.ml_ensembles.find_one({"user_id": uid}))["production"]["digest"]

            def refused(approvals=None, **kw):
                if approvals is not None:
                    mm.sign(approvals, promote=[uid], **kw)
                with pytest.raises(HTTPException) as e:
                    _run(me.promote_candidate(db, uid, "ops@x"))
                assert e.value.status_code in (404, 409) and prod_digest() == pd
                return e.value.detail
            refused()                                                       # no manifest naming the candidate
            # approvals: distinct PRINCIPALS, exact digest, bytes present in the store
            a1 = _run(me.approve_candidate(db, uid, f"a-{uid}@x", "reviewed", actor_id=str(p1)))["approval"]
            with pytest.raises(HTTPException) as e:
                _run(me.approve_candidate(db, uid, f"a-again-{uid}@x", "same principal, other email", actor_id=str(p1)))
            assert e.value.detail["code"] == "repeated_approver"
            a2 = _run(me.approve_candidate(db, uid, f"b-{uid}@x", "second", actor_id=str(p2)))["approval"]
            good = [a1, a2]
            # (1) two emails pointing at events from ONE principal
            with pytest.raises(mm.ModelRefused):
                mm.sign([a1, {**a2, "principal_id": a1["principal_id"]}], promote=[uid])
            # (2) forged event id / (3) event of another target with same digest / (4) approval absent from candidate
            refused([a1, {**a2, "audit_event_id": "0" * 64}])
            other = _run(db.admin_audit_log.find_one({"entry_hash": a2["audit_event_id"]}))
            other = {**other, "_id": ObjectId(), "target_id": uid + "-other"}
            _run(db.admin_audit_log.insert_one(other))                      # cloned event, wrong target (chain now also tampered)
            d = refused([a1, {**a2, "audit_event_id": other["entry_hash"]}])
            assert any("does not match" in p or "chain" in p or "differ" in p for p in d["problems"])
            _run(db.admin_audit_log.delete_one({"_id": other["_id"]}))
            d = refused([a1, {**a2, "email": f"other-{uid}@x"}])           # manifest identity != authenticated actor
            assert any("does not match" in p for p in d["problems"])
            refused(good, commit="1" * 40)                                  # stale build
            # bytes absent from the shared store → refused, previous production stays active
            _run(db.fs_model_artifacts_index.delete_one({"_id": cd}))
            for f in _run(db["model_artifacts.files"].find({"filename": cd}).to_list(10)):
                _run(db["model_artifacts.chunks"].delete_many({"files_id": f["_id"]}))
            _run(db["model_artifacts.files"].delete_many({"filename": cd}))
            (model_dir / "_cache" / "sha256" / f"{cd}.joblib").unlink(missing_ok=True)
            d = refused(good)
            assert any("shared artifact store" in p for p in d["problems"])
            _run(ms.put(db, cand_bytes, cd, uid, _prov(mm, {"sha256": cd})))
            # DB failure at the pointer flip → nothing activated, candidate retriable
            mm.sign(good, promote=[uid])

            class _Col:
                def __init__(self, real):
                    self.real = real

                def __getattr__(self, k):
                    return getattr(self.real, k)

                async def update_one(self, *a, **k):
                    raise TimeoutError("mongo down")

            class _DB:
                def __init__(self):
                    self.ml_ensembles = _Col(db.ml_ensembles)

                def __getattr__(self, k):
                    return getattr(db, k)

                def __getitem__(self, k):
                    return db[k]
            real_fetch = ms.fetch
            monkeypatch.setattr(ms, "fetch", lambda _db, digest, mdir: real_fetch(db, digest, mdir))
            with pytest.raises(HTTPException) as e:
                _run(me.promote_candidate(_DB(), uid, "ops@x"))
            monkeypatch.setattr(ms, "fetch", real_fetch)
            assert e.value.status_code == 503 and prod_digest() == pd
            assert _run(db.ml_ensembles.find_one({"user_id": uid}))["candidate"]["status"] == "awaiting_approval"
            assert _run(ms.exists(db, cd))
            # success: ONE atomic pointer flip + exactly one promotion audit event; candidate bytes preserved
            st = _run(me.promote_candidate(db, uid, f"b-{uid}@x"))
            assert st["production"]["digest"] == cd and st["candidate"] is None and st["production"]["aucs"] == {"xgboost": 0.7}
            doc = _run(db.ml_ensembles.find_one({"user_id": uid}))
            assert doc["promotion_outbox"] is None and doc["previous_production"]["digest"] == pd
            assert _run(db.admin_audit_log.count_documents({"action": "model_promoted", "target_id": uid})) == 1
            assert _run(me.flush_promotion_outbox(db, uid)) == 0             # idempotent
            assert _run(ms.exists(db, cd)) and _run(ms.exists(db, pd))
            # a second "instance" (empty cache, different MODEL_DIR) resolves the identical digest from the store
            me._models_cache.clear()
            known = _run(me.approval_events(db, good))
            models, digest = _run(me._load_production(db, uid, doc["production"], known))
            assert digest == cd and (model_dir / "_cache" / "sha256" / f"{cd}.joblib").exists()
        finally:
            _run(db.ml_ensembles.delete_many({"user_id": uid}))
            _run(db.admin_audit_log.delete_many({"target_id": uid}))
            _run(db.users.delete_many({"_id": {"$in": [p1, p2]}}))

    def test_pipeline_and_api_share_one_state_shape(self):
        src = open(os.path.join(_BACKEND_DIR, "learning_pipeline.py")).read()
        assert '"status": "promoted"' not in src and "candidate_ready_for_review" in src and "approved_by\": None" in src
        routes = open(os.path.join(_BACKEND_DIR, "routes", "ml_routes.py")).read()
        assert "public_state" in routes and "/candidate/approve" in routes and "/candidate/promote" in routes
        ui = open(os.path.join(ROOT, "frontend", "src", "components", "LearningPipelineCard.jsx")).read()
        assert "ml-production-state" in ui and "ml-candidate-state" in ui
        import ml_ensemble as me
        st = me.public_state({"candidate": {"status": "awaiting_approval", "digest": "a"}, "production": None})
        assert st["status"] == "no_production_model" and st["promotion"] == "candidate_ready_for_review"


class TestReleaseProvenance:
    def test_model_dir_is_repo_relative_and_archive_gate_verifies(self, tmp_path):
        import model_manifest as mm
        assert mm.MODEL_DIR == (mm._HERE / "models_store") or os.environ.get("MODEL_DIR")
        # exact verify_release.sh step from an arbitrary directory with no /app tree and no .git
        arch = tmp_path / "archive"
        (arch / "backend").mkdir(parents=True)
        for f in ("model_manifest.py", "release_signing.py", "app_env.py", "ml_ensemble.py", "pip_utils.py",
                  "rl_policy.py", "ml_runtime.py"):
            shutil.copy(os.path.join(_BACKEND_DIR, f), arch / "backend" / f)
        shutil.copytree(os.path.join(_BACKEND_DIR, "models_store"), arch / "backend" / "models_store",
                        ignore=shutil.ignore_patterns("_quarantine"))
        from release_signing import public_key_b64
        env = {**os.environ, "RELEASE_PUBLIC_KEY_B64": public_key_b64(), "PYTHONPATH": str(arch / "backend")}
        env.pop("MODEL_DIR", None)
        step = "cd backend && python -m model_manifest verify"
        r = subprocess.run(["bash", "-c", step], cwd=arch, env=env, capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.startswith("OK:"), r.stdout + r.stderr
        # wrong pinned key → refused
        r = subprocess.run(["bash", "-c", step], cwd=arch, env={**env, "RELEASE_PUBLIC_KEY_B64": "A" * 43 + "="},
                           capture_output=True, text=True)
        assert r.returncode != 0 and "REFUSED" in (r.stderr + r.stdout)

    def test_consistency_check_reports_every_disagreeing_field(self, tmp_path):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import release_consistency_check as rc
        sha, other = "a" * 40, "b" * 40
        root = tmp_path
        (root / "backend" / "models_store").mkdir(parents=True)
        (root / "release").mkdir()
        (root / "docs").mkdir()
        (root / "backend" / "BUILD_SHA").write_text(sha + "\n")
        (root / "docs" / "TEST_MANIFEST.md").write_text("# 1 tests\n")
        (root / "backend" / "models_store" / "MODEL_MANIFEST.json").write_text(json.dumps({"body": {"code_commit": sha}}))
        (root / "docs" / "RELEASE_SUMMARY.md").write_text(f"- Source commit: `{sha}`\n")
        lock = {"git_commit": sha, "source_sha": sha, "authoritative": True,
                "test_manifest_sha256": rc._sha256(str(root / "docs" / "TEST_MANIFEST.md")),
                "model_manifest_sha256": rc._sha256(str(root / "backend" / "models_store" / "MODEL_MANIFEST.json")),
                "images": {"backend": "sha256:1", "frontend": "sha256:2"}}
        (root / "release" / "rc_lock.json").write_text(json.dumps(lock))
        facts = rc.gather(str(root), commit=sha, backend_digest="sha256:1", frontend_digest="sha256:2")
        assert rc.compare(facts, strict=True) == ([], [])
        # one-byte test change → test_manifest_sha256 disagrees; manifest commit drift → named field
        (root / "docs" / "TEST_MANIFEST.md").write_text("# 2 tests\n")
        (root / "backend" / "models_store" / "MODEL_MANIFEST.json").write_text(json.dumps({"body": {"code_commit": other}}))
        mism, _ = rc.compare(rc.gather(str(root), commit=sha), strict=True)
        assert any(m.startswith("test_manifest_sha256") for m in mism)
        assert any(m.startswith("model_manifest.code_commit") for m in mism)
        assert any(m.startswith("model_manifest_sha256") for m in mism)
        # developer snapshot: digests strict, commits only warned
        lock["authoritative"] = False
        (root / "release" / "rc_lock.json").write_text(json.dumps(lock))
        (root / "docs" / "TEST_MANIFEST.md").write_text("# 1 tests\n")
        mism, warn = rc.compare(rc.gather(str(root)), strict=False)
        assert any(m.startswith("model_manifest_sha256") for m in mism) and any("model_manifest.code_commit" in w for w in warn)
        # the repository itself must be consistent at the developer-snapshot level
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "release_consistency_check.py")],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
        assert "model_manifest resign --commit" in rel and "release_consistency_check.py --root /tmp/pkg --strict" in rel
        assert rel.index("release_consistency_check.py --root /tmp/pkg --strict") < rel.index('tar -czf "stoic-${REF}.tar.gz"')

    def test_release_summary_check_is_field_level_and_archive_safe(self, tmp_path):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import generate_release_summary as gs
        f = gs.fields()
        text = gs.render(f)
        assert gs.check(text, f, strict_source=True) == []
        for k, v in (("source_commit", "0" * 40), ("lock_authoritative", "True"), ("model_manifest_sha256", "x"),
                     ("test_manifest_sha256", "y"), ("keepalive_seconds", "650")):
            diffs = gs.check(gs.render({**f, k: v}), f, strict_source=True)
            assert len(diffs) == 1 and diffs[0].startswith(k + ":"), (k, diffs)
        # archive without .git: source identity comes from BUILD_SHA, never a traceback
        import importlib
        orig = gs.ROOT
        try:
            (tmp_path / "backend").mkdir()
            (tmp_path / "backend" / "BUILD_SHA").write_text("c" * 40)
            gs.ROOT = str(tmp_path)
            assert gs._sha() == "c" * 40
        finally:
            gs.ROOT = orig
            importlib.reload(gs)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "generate_release_summary.py"), "--check"],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        pointer = open(os.path.join(ROOT, ".emergent", "summary.txt")).read()
        assert "650" not in pointer and "production-ready" not in pointer and "RELEASE_SUMMARY.md" in pointer
        assert len(pointer.splitlines()) < 20


class TestCanonicalSnapshot:
    def test_persisted_snapshot_is_shared_and_invalidated(self, monkeypatch):
        import canonical_decision as cd
        db = _db()
        uid = "r12-cd-" + uuid.uuid4().hex[:6]
        calls = {"n": 0}

        async def _platform(_db):
            calls["n"] += 1
            return cd.from_snapshot({"level": "FULL", "domains": {}, "snapshot_id": "s"})
        monkeypatch.setattr(cd, "decide_platform", _platform)
        try:
            d1 = _run(cd.decide_user(db, uid))
            d2 = _run(cd.decide_user(db, uid))                       # "another worker": no process-local state
            assert d1["decision_id"] == d2["decision_id"] and d1["input_version"] == d2["input_version"] and calls["n"] == 1
            v = _run(cd.bump_authority_version(db, "panic"))
            d3 = _run(cd.decide_user(db, uid))
            assert d3["decision_id"] != d1["decision_id"] and d3["input_version"] == v and calls["n"] == 2
            assert cd.denial(d3, "/x")["input_version"] == v
            # an inventory change (new account/bot) invalidates without an explicit bump
            acc = _run(db.accounts.insert_one({"user_id": uid, "mode": "demo", "trading_enabled": False, "label": "r12cd"}))
            try:
                d4 = _run(cd.decide_user(db, uid))
                assert d4["decision_id"] != d3["decision_id"] and calls["n"] == 3
                assert _run(cd.decide_user(db, uid))["decision_id"] == d4["decision_id"]
            finally:
                _run(db.accounts.delete_one({"_id": acc.inserted_id}))
        finally:
            _run(db.canonical_decisions.delete_one({"_id": uid}))
        for f, hook in (("routes/panic_routes.py", "bump_authority_version"), ("trading_authority.py", "bump_authority_version"),
                        ("trade_reconciler.py", "bump_authority_version"), ("inventory_projection.py", "bump_authority_version"),
                        ("turnstile_break_glass.py", "bump_authority_version")):
            assert hook in open(os.path.join(_BACKEND_DIR, f)).read(), f
        assert "_snapshots" not in open(os.path.join(_BACKEND_DIR, "canonical_decision.py")).read()


class TestTurnstileTypedClaims:
    def test_fuzzed_claim_types_never_500_or_degrade(self, monkeypatch):
        import turnstile_gate as tg
        import httpx

        class _Resp:
            status_code = 200

            def __init__(self, p):
                self.p = p

            def json(self):
                return self.p

        class _Client:
            resp = None

            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, data=None):
                return _Client.resp
        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "k")
        monkeypatch.setenv("TURNSTILE_EXPECTED_HOSTNAMES", "app.example.com")
        monkeypatch.setattr(tg, "_mark_provider_failure", lambda: pytest.fail("typed-claim fault marked provider degraded"))
        good = {"success": True, "hostname": "app.example.com", "action": "login",
                "challenge_ts": datetime.now(timezone.utc).isoformat(), "error-codes": []}
        _Client.resp = _Resp(good)
        assert _run(tg.verify_token("tok", "1.1.1.1", action="login"))["ok"] is True
        for claim in ("success", "hostname", "action", "challenge_ts", "error-codes"):
            for bad in (None, True, 7, [1], {"a": 1}):
                _Client.resp = _Resp({**good, claim: bad})
                r = _run(tg.verify_token("tok", "1.1.1.1", action="login"))
                assert r["outage"] is False, (claim, bad, r)
                if (claim, bad) in (("success", True), ("error-codes", None)):
                    assert r["ok"] is True, (claim, bad, r)
                elif (claim, bad) == ("success", None):
                    assert r["ok"] is False
                else:
                    assert r["ok"] is False and r["state"] == "client_token_invalid", (claim, bad, r)


class TestBreakGlassErrors:
    def test_duplicate_vs_infrastructure_vs_malformed(self, monkeypatch):
        import turnstile_break_glass as bg
        from pymongo.errors import DuplicateKeyError
        db = _db()
        _run(db.platform_state.delete_many({"_id": {"$in": [bg.PENDING_ID, bg.DOC_ID]}}))
        payload = {"incident_id": "INC-12", "approver": "second@x", "reason": "provider outage during login window",
                   "scope": ["login"], "ttl_minutes": 10}

        class _Col:
            def __init__(self, exc):
                self.exc = exc

            async def delete_one(self, *a, **k):
                return None

            async def insert_one(self, *a, **k):
                raise self.exc

        class _DB:
            def __init__(self, exc):
                self.platform_state = _Col(exc)
                self.alerts = db.alerts
                self.admin_audit_log = db.admin_audit_log

            def __getattr__(self, k):
                return getattr(db, k)
        monkeypatch.setattr(bg, "active", _fake_none)
        with pytest.raises(HTTPException) as e:
            _run(bg.request_activation(_DB(DuplicateKeyError("dup")), payload, "first@x"))
        assert e.value.status_code == 409
        with pytest.raises(HTTPException) as e:
            _run(bg.request_activation(_DB(TimeoutError("mongo down")), payload, "first@x"))
        assert e.value.status_code == 503 and e.value.detail["code"] == "break_glass_store_unavailable"
        # malformed expiry blocks promotion, is visible, never 500s, cannot be approved
        _run(db.platform_state.insert_one({"_id": bg.PENDING_ID, **payload, "actor": "first@x", "expires_at": "not-a-date"}))
        try:
            s = _run(bg.status(db))
            assert s["promotion_blocked"] and s["pending_request"]["malformed"] is True
            assert _run(bg.readiness_check(db))["ok"] is False
            with pytest.raises(HTTPException) as e:
                _run(bg.approve_activation(db, "second@x"))
            assert e.value.detail["code"] == "break_glass_request_malformed"
            _run(db.platform_state.update_one({"_id": bg.PENDING_ID}, {"$set": {"expires_at": "2026-01-01T00:00:00"}}))  # naive
            assert _run(bg.status(db))["pending_request"]["malformed"] is True
        finally:
            _run(db.platform_state.delete_one({"_id": bg.PENDING_ID}))


async def _fake_none(db):
    return None


class TestInventoryTwoAdmin:
    def test_hash_approval_requires_second_admin_and_recompute(self):
        import inventory_projection as ip
        db = _db()
        uid = "r12-inv-" + uuid.uuid4().hex[:6]
        _run(db.platform_state.delete_one({"_id": ip.HASH_PENDING_ID}))
        try:
            p = _run(ip.approve_current(db, "admin-a@x", "propose current", uid))
            assert p["pending_approval"]["proposed_by"] == "admin-a@x" and p["approved_hash"] is None
            with pytest.raises(HTTPException) as e:
                _run(ip.confirm_current(db, "admin-a@x"))                 # proposer cannot confirm
            assert e.value.status_code == 403
            # inventory changed since proposal → proposal invalidated
            acc = _run(db.accounts.insert_one({"user_id": uid, "mode": "demo", "trading_enabled": False, "label": "r12"}))
            with pytest.raises(HTTPException) as e:
                _run(ip.confirm_current(db, "admin-b@x"))
            assert e.value.status_code == 409 and e.value.detail["proposal_invalidated"]
            assert _run(ip.pending_hash_approval(db)) is None
            _run(ip.approve_current(db, "admin-a@x", "re-propose", uid))
            p = _run(ip.confirm_current(db, "admin-b@x"))
            assert p["approved_hash"] == p["inventory_hash"]
            ev = _run(db.inventory_config_events.find_one({"inventory_hash": p["inventory_hash"]}, sort=[("at", -1)]))
            assert ev["actor"] == "admin-a@x" and ev["approved_by"] == "admin-b@x"
            _run(db.accounts.delete_one({"_id": acc.inserted_id}))
            _run(db.inventory_config_events.delete_many({"actor": "admin-a@x"}))
        finally:
            _run(db.platform_state.delete_one({"_id": ip.HASH_PENDING_ID}))
            _run(db.accounts.delete_many({"user_id": uid}))
        src = open(os.path.join(_BACKEND_DIR, "routes", "authority_routes.py")).read()
        assert "/inventory/approve/confirm" in src and "confirm_current" in src

    def test_policy_migration_is_complete_and_single_use(self):
        import inventory_projection as ip
        from release_signing import sign_hex
        db = _db()
        now = datetime.now(timezone.utc)
        mig = {"schema": ip.MIGRATION_SCHEMA, "installation_id": ip.installation_id(), "environment": ip.environment_label(),
               "previous_policy_version": ip.DEPLOYMENT_POLICY_VERSION, "policy_version": "4/4/4-v2",
               "accounts": 4, "enabled": 4, "bots": 4, "account_ids": list("abcd"), "reason": "four live desks approved",
               "issuer": "release-ops", "issued_at": now.isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat(),
               "nonce": uuid.uuid4().hex}
        mig["signature_hex"] = sign_hex(ip.migration_body(mig))
        payload = {"accounts": 4, "enabled": 4, "bots": 4, "account_ids": list("abcd"), "policy_migration": mig}
        assert ip.migration_problems(payload, current_policy_version=ip.DEPLOYMENT_POLICY_VERSION) == []
        assert ip.migration_problems(payload, current_policy_version="4/4/4-v2")           # wrong previous version
        assert ip.migration_problems({**payload, "account_ids": list("abce")}, current_policy_version=ip.DEPLOYMENT_POLICY_VERSION)
        assert ip.migration_problems(payload, current_policy_version=ip.DEPLOYMENT_POLICY_VERSION,
                                     now=now + timedelta(hours=2))                          # expired
        _run(ip.consume_migration_nonce(db, mig))
        with pytest.raises(HTTPException) as e:
            _run(ip.consume_migration_nonce(db, mig))                                       # replay
        assert e.value.detail["code"] == "policy_migration_replayed"
        _run(db.policy_migration_nonces.delete_one({"_id": mig["nonce"]}))


class TestRound13:
    def test_public_status_separates_infrastructure_from_trading(self):
        src = open(os.path.join(_BACKEND_DIR, "routes", "portal_routes.py")).read()
        assert '"trading": trading' in src and '"connectivity": connectivity' in src and 'account' not in src.split('trading = {')[1].split('}')[0].lower().replace("enabled_accounts_present", "")
        assert 'readiness["state"] == "READY" and connectivity == "active"' in src      # "Trading ready" only when canonical READY + fresh terminals
        login = open(os.path.join(ROOT, "frontend", "src", "pages", "Login.jsx")).read()
        assert "SYSTEM OPERATIONAL" not in login and "headline" in login
        assert os.path.exists(os.path.join(ROOT, "docs", "TURNSTILE_PREVIEW.md"))

    def test_canonical_snapshot_single_flight_and_input_hash(self, monkeypatch):
        import canonical_decision as cd
        db = _db()
        uid = "r13-cd-" + uuid.uuid4().hex[:6]
        calls = {"n": 0}

        async def _platform(_db):
            calls["n"] += 1
            return cd.from_snapshot({"level": "FULL", "domains": {}, "snapshot_id": "s"})
        monkeypatch.setattr(cd, "decide_platform", _platform)
        try:
            # concurrent cold-cache requests → ONE persisted decision for the version
            results = _run(asyncio.gather(*(cd.decide_user(db, uid, fresh=(i == 0)) for i in range(5))))
            ids = {r["decision_id"] for r in results}
            persisted = _run(db.canonical_decisions.find_one({"_id": uid}))["decision"]["decision_id"]
            assert persisted in ids and _run(cd.decide_user(db, uid))["decision_id"] == persisted
            d = _run(cd.decide_user(db, uid))
            assert len(d["input_hash"]) == 16 and cd.denial(d, "/x")["input_hash"] == d["input_hash"]
            # platform authority change is part of the fingerprint → immediate invalidation
            _run(db.platform_state.update_one({"_id": "trading_authority"}, {"$set": {"set_at": "r13-" + uuid.uuid4().hex}}, upsert=True))
            assert _run(cd.decide_user(db, uid))["input_hash"] != d["input_hash"]
        finally:
            _run(db.canonical_decisions.delete_one({"_id": uid}))

    def test_release_pipeline_stages_one_tree_before_build(self):
        rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
        i_stage = rel.index("re-sign model manifest IN the staged tree")
        i_build = rel.index("docker build -f /tmp/pkg/Dockerfile.backend")
        i_extract = rel.index("Extract provenance from the candidate image BEFORE push")
        i_push = rel.index("Publish images to GHCR")
        assert i_stage < i_build < i_extract < i_push
        assert "archive manifest != published image manifest" in rel
        assert rel.count("model_manifest resign --commit") >= 2          # hermetic job stages the same way
        assert "model_manifest verify --build" in rel
        src = open(os.path.join(_BACKEND_DIR, "ml_ensemble.py")).read()
        assert "os.replace(cand_file" not in src and "promotion_outbox" in src and "model_store.fetch" in src


class TestBrokerStatementLedger:
    def _statement(self, acc_id, **over):
        from broker_statement_ledger import STATEMENT_SCHEMA
        now = datetime.now(timezone.utc)
        st = {"schema": STATEMENT_SCHEMA, "statement_id": "ST-" + uuid.uuid4().hex[:8], "account_id": acc_id,
              "broker_login": "1001", "currency": "USD",
              "period_from": (now - timedelta(days=30)).isoformat(), "period_to": (now - timedelta(days=1)).isoformat(),
              "issuer": "Broker Ltd", "issued_at": now.isoformat(),
              "opening_balance": 10000.00, "trading_pnl": 18.02, "commission": -0.70, "swap": 0.25,
              "deposits": 500.00, "withdrawals": 100.00, "corrections": 0.00, "fx_conversion": 0.00,
              "closing_balance": 10417.57, "unrealized_pnl": -12.34, "closing_equity": 10405.23}
        st.update(over)
        return st

    def test_signed_statement_reconciles_to_the_cent_and_gates_attestation(self, monkeypatch):
        import base64
        import broker_statement_ledger as bl
        from bson import ObjectId
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        from release_signing import sign_hex as release_sign
        # SEC-001: statements are authenticated by an INDEPENDENT broker/statement key — never the release key
        broker_key = Ed25519PrivateKey.generate()
        broker_pub = base64.b64encode(broker_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()

        def sign_hex(body: bytes) -> str:
            return broker_key.sign(body).hex()
        db = _db()
        uid = "r13-led-" + uuid.uuid4().hex[:6]
        acc = ObjectId()
        _run(db.accounts.insert_one({"_id": acc, "user_id": uid, "label": "live-1", "mode": "live", "trading_enabled": True,
                                     "broker_environment": "LIVE", "account_type": "real", "server": "Broker-Live", "broker": "Broker Ltd",
                                     "verified_identity": {"account_number": "1001", "broker_server": "Broker-Live"}}))
        when = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        _run(db.broker_deals.insert_many([
            {"user_id": uid, "account_id": str(acc), "deal_id": "1", "profit": "18.02", "commission": "-0.70", "swap": "0.25",
             "deal_entry": "out", "occurred_at": when, "financial_reconciliation_status": "complete"}]))
        _run(db.account_cashflows.insert_many([{"user_id": uid, "account_id": str(acc), "kind": "deposits", "amount": 500, "at": when},
                                               {"user_id": uid, "account_id": str(acc), "kind": "withdrawals", "amount": 100, "at": when}]))
        try:
            st = self._statement(str(acc))
            sig = sign_hex(bl.statement_body(st))
            monkeypatch.delenv("STATEMENT_ATTESTATION_PUBLIC_KEY_B64", raising=False)
            assert any("not configured" in p for p in bl.statement_problems(st, sig))            # absent key → fail closed
            from release_signing import public_key_b64
            monkeypatch.setenv("STATEMENT_ATTESTATION_PUBLIC_KEY_B64", public_key_b64())
            assert any("differ from the platform release key" in p
                       for p in bl.statement_problems(st, release_sign(bl.statement_body(st))))   # release key refused
            monkeypatch.setenv("STATEMENT_ATTESTATION_PUBLIC_KEY_B64", broker_pub)
            assert "signature" in " ".join(bl.statement_problems(st, release_sign(bl.statement_body(st))))
            assert bl.statement_problems({**st, "account_id": {"$ne": ""}}, sig)                  # operator-shaped id refused
            missing = {k: v for k, v in st.items() if k != "opening_balance"}
            assert any("opening_balance required" in p for p in bl.statement_problems(missing, sig))   # no KeyError/500
            assert bl.statement_problems(st, sig) == []
            assert bl.statement_problems({**st, "closing_balance": 10417.58}, sig)          # one cent off → does not balance / bad sig
            assert "signature" in " ".join(bl.statement_problems(st, "00" * 64))
            assert _run(bl.ledger_gate(db, uid)) == ["STATEMENT_LEDGER_MISSING"]
            row = _run(bl.reconcile(db, uid, st, sig, "ops@x"))
            assert row["status"] == "RECONCILED" and row["discrepancies"] == [] and row["return_formula_version"] == bl.RETURN_FORMULA_VERSION
            assert row["return_pct"] == round((10405.23 - 10000 - 400) / (10000 + 200) * 100, 4)
            assert _run(bl.ledger_gate(db, uid)) == []
            # a one-cent platform/statement difference is a DISCREPANCY that withholds attestation
            # r14 P1-03 — identity / coverage / append-only guarantees
            now = datetime.now(timezone.utc)
            assert "broker_login required" in bl.statement_problems({**st, "broker_login": ""}, sig)
            assert any("shorter" in p for p in bl.statement_problems({**st, "period_from": (now - timedelta(days=2)).isoformat()}, sig))
            assert any("future" in p for p in bl.statement_problems({**st, "issued_at": (now + timedelta(days=1)).isoformat()}, sig))
            for bad, needle in (({"broker_login": "9999"}, "verified broker identity"), ({"issuer": "Unknown Corp"}, "registered broker")):
                stx = self._statement(str(acc), statement_id="STX", **bad)
                with pytest.raises(Exception) as ei:
                    _run(bl.reconcile(db, uid, stx, sign_hex(bl.statement_body(stx)), "ops@x"))
                assert needle in str(ei.value.detail)
            # overwrite of the same statement_id with different bytes is refused; same bytes idempotent
            assert _run(bl.reconcile(db, uid, st, sig, "ops@x"))["ledger_seq"] == row["ledger_seq"]
            st_mod = self._statement(str(acc), statement_id=st["statement_id"], swap=0.26, closing_balance=10417.58, closing_equity=10405.24)
            with pytest.raises(Exception) as ei:
                _run(bl.reconcile(db, uid, st_mod, sign_hex(bl.statement_body(st_mod)), "ops@x"))
            assert ei.value.status_code == 409
            # overlap and gap against the accepted period are refused
            for pf, pt, needle in ((now - timedelta(days=20), now - timedelta(days=1), "overlaps"),
                                   (now - timedelta(days=60), now - timedelta(days=40), "gap")):
                stg = self._statement(str(acc), statement_id="STG", period_from=pf.isoformat(), period_to=pt.isoformat(),
                                      issued_at=now.isoformat())
                with pytest.raises(Exception) as ei:
                    _run(bl.reconcile(db, uid, stg, sign_hex(bl.statement_body(stg)), "ops@x"))
                assert needle in str(ei.value.detail), (needle, ei.value.detail)
            assert row["ledger_root"] and row["ledger_root_sig"]
            _run(db.reconciliation_ledger.delete_many({"user_id": uid}))
            st2 = self._statement(str(acc), swap=0.26, closing_balance=10417.58, closing_equity=10405.24)
            row2 = _run(bl.reconcile(db, uid, st2, sign_hex(bl.statement_body(st2)), "ops@x"))
            assert row2["status"] == "DISCREPANCY" and row2["discrepancies"][0]["field"] == "swap" and row2["discrepancies"][0]["delta"] == 0.01
            assert "STATEMENT_DISCREPANCY" in _run(bl.ledger_gate(db, uid))
            # unreconciled deal → UNRECONCILED_DEALS; stale period → STATEMENT_PERIOD_STALE
            _run(db.broker_deals.update_one({"user_id": uid, "deal_id": "1"}, {"$set": {"financial_reconciliation_status": "pending"}}))
            _run(db.reconciliation_ledger.delete_many({"user_id": uid}))
            st3 = self._statement(str(acc))
            assert _run(bl.reconcile(db, uid, st3, sign_hex(bl.statement_body(st3)), "ops@x"))["status"] == "UNRECONCILED_DEALS"
            old = datetime.now(timezone.utc) - timedelta(days=90)
            st4 = self._statement(str(acc), period_from=(old - timedelta(days=30)).isoformat(), period_to=old.isoformat(),
                                  trading_pnl=0.0, commission=0.0, swap=0.0, deposits=0.0, withdrawals=0.0,
                                  closing_balance=10000.0, unrealized_pnl=0.0, closing_equity=10000.0)
            _run(db.reconciliation_ledger.delete_many({"user_id": uid}))
            _run(bl.reconcile(db, uid, st4, sign_hex(bl.statement_body(st4)), "ops@x"))
            assert "STATEMENT_PERIOD_STALE" in _run(bl.ledger_gate(db, uid))
            src = open(os.path.join(_BACKEND_DIR, "routes", "performance_routes.py")).read()
            assert "ledger_gate" in src and "STATEMENT_LEDGER_UNAVAILABLE" in src
        finally:
            for c in ("accounts", "broker_deals", "account_cashflows", "reconciliation_ledger"):
                _run(db[c].delete_many({"user_id": uid}))
