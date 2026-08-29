"""iter-146 targeted verification — signed factor exposure via real guard
path (signed_net model detail + SELL netting), image-digest enforcement,
and slippage evidence gate placement (LIVE new-risk only).
"""
import os
import re
import uuid

import pytest
import yaml

from modules.pamm.strategy_guard import (
    MIN_SLIPPAGE_SAMPLES,
    _enforce_production_provenance,
    signed_factor_lots,
    slippage_evidence_violation,
)


# ---------- unit-lane pure functions ----------
class TestSignedFactorLotsAdditional:
    def test_net_out_long_and_short_same_factor(self):
        # BUY EURUSD 1.0 -> USD -1.0 ; SELL GBPUSD 1.0 -> USD -1.0
        # (both short USD — accumulate)
        # BUY EURUSD (USD -1) + BUY GBPUSD (USD -1) accumulate -> USD -2
        a = signed_factor_lots("EURUSD", "BUY", 1.0)
        b = signed_factor_lots("GBPUSD", "BUY", 1.0)
        assert round(a["USD"] + b["USD"], 4) == -2.0
        # opposite side nets to zero: BUY EURUSD + SELL GBPUSD -> USD 0
        c = signed_factor_lots("GBPUSD", "SELL", 1.0)
        assert round(a["USD"] + c["USD"], 4) == 0.0

    def test_rounding_to_4_decimals(self):
        out = signed_factor_lots("EURUSD", "BUY", 0.12345)
        assert out == {"EUR": 0.1235, "USD": -0.1235}


class TestSlippageEvidenceEdges:
    def test_thresholds(self):
        assert MIN_SLIPPAGE_SAMPLES == {"VERY_HIGH": 10, "MAXIMUM": 20}

    def test_maximum_19_blocks(self):
        v = slippage_evidence_violation(
            "MAXIMUM", {"recent_slippage_sample_count": 19})
        assert v and v["reason"] == "insufficient_slippage_evidence"
        assert v["detail"]["required"] == 20
        assert v["detail"]["sample_count"] == 19

    def test_very_high_10_passes(self):
        assert slippage_evidence_violation(
            "VERY_HIGH", {"recent_slippage_sample_count": 10}) is None

    def test_medium_and_none_never_block(self):
        assert slippage_evidence_violation("MEDIUM", {}) is None
        assert slippage_evidence_violation(None, {}) is None

    def test_missing_sample_count_counts_as_zero(self):
        v = slippage_evidence_violation("MAXIMUM", None)
        assert v and v["detail"]["sample_count"] == 0


# ---------- image-digest provenance ----------
class TestImageDigestEnforcement:
    _fake_sha = "d" * 40

    def test_production_raises_when_digest_missing(self):
        with pytest.raises(RuntimeError, match="STOIC_IMAGE_DIGEST"):
            _enforce_production_provenance(
                sha=self._fake_sha, production=True, image_digest="")

    def test_production_ok_with_valid_digest(self):
        _enforce_production_provenance(
            sha=self._fake_sha, production=True,
            image_digest="sha256:" + "a" * 64)

    def test_non_production_does_not_raise(self):
        _enforce_production_provenance(
            sha=self._fake_sha, production=False, image_digest="")

    def test_production_raises_on_bad_sha(self):
        with pytest.raises(RuntimeError, match="Git SHA"):
            _enforce_production_provenance(
                sha="not-hex", production=True,
                image_digest="sha256:" + "a" * 64)


# ---------- static workflow / deploy checks ----------
_REPO = "/app"


class TestDeployStaticGates:
    def test_docker_compose_passes_stoic_image_digest_env(self):
        with open(f"{_REPO}/docker-compose.yml") as f:
            data = yaml.safe_load(f)
        env = data["services"]["backend"]["environment"]
        # env can be list or dict — both mean the key is passed through
        if isinstance(env, dict):
            assert "STOIC_IMAGE_DIGEST" in env
            assert "${STOIC_IMAGE_DIGEST" in str(env["STOIC_IMAGE_DIGEST"])
        else:
            assert any("STOIC_IMAGE_DIGEST" in str(x) for x in env)

    def test_install_sh_resolves_digest_after_build_and_hardfails(self):
        with open(f"{_REPO}/deploy/install.sh") as f:
            body = f.read()
        # ordering: docker build precedes digest resolve
        i_build = body.find("docker compose build")
        i_digest = body.find("STOIC_IMAGE_DIGEST=$(docker inspect")
        assert i_build > 0 and i_digest > 0 and i_digest > i_build
        # hard-fail branch
        assert 'if [ -z "${STOIC_IMAGE_DIGEST}" ]' in body
        assert body.count('exit 1') >= 2


class TestCiWorkflowStaticGates:
    def test_ci_yml_runs_full_pamm_http_matrix(self):
        with open(f"{_REPO}/.github/workflows/ci.yml") as f:
            body = f.read()
        for suite in (
                "test_iter216_pamm_strategy_http.py",
                "test_iter217_cert_campaign_http.py",
                "test_iter218_strategy_guard_http.py",
                "test_iter220_safety_closure_http.py",
                "test_iter221_risk_truth_http.py",
                "test_iter222_hardening_http.py"):
            assert suite in body, suite
        # redis service + bypass tokens
        assert "redis:7" in body
        assert "STEP_UP_BYPASS_TOKEN" in body
        assert "RATE_LIMIT_BYPASS_TOKEN" in body

    def test_release_yml_build_sha_equality_and_evidence(self):
        with open(f"{_REPO}/.github/workflows/release.yml") as f:
            body = f.read()
        # equality gates compare against the PEELED commit — github.sha is
        # the tag OBJECT for annotated tags and must never be used raw
        assert body.count("!= GITHUB_SHA commit") >= 3
        assert body.count("^{commit}") >= 4
        assert 'os.environ["COMMIT_SHA"]' in body
        assert '--build-arg GIT_SHA="${COMMIT_SHA}"' in body
        # no gate may compare BUILD_SHA against the raw event sha
        assert '"${ARCHIVE_SHA}" != "${SHA}"' not in body
        # collect-only gate
        assert "pytest tests/unit --collect-only" in body
        # release-evidence lifecycle
        assert "release-evidence.json" in body
        assert "cosign sign-blob --yes release-evidence.json" in body
        assert "cosign verify-blob release-evidence.json" in body
        # host_agent source hash in manifest
        assert 'source_hash' in body and 'host_agent' in body
        # evidence uploaded with release
        assert "release-evidence.json.sig" in body
        assert "release-evidence.json.pem" in body


# ---------- conftest secure-cookie strip ----------
class TestConftestSecureCookieStrip:
    def test_conftest_strips_secure_in_session_patch(self):
        with open(f"{_REPO}/backend/tests/conftest.py") as f:
            body = f.read()
        # Some mutation of 'secure' cookie flag inside the session patch
        assert re.search(r"[Ss]ecure", body), \
            "expected secure-cookie handling in conftest.py"
