"""iter-132 — deployment guard: heavy ML deps must NEVER return to
requirements.txt (torch stack OOMs the 1Gi production pod — recurred twice:
iter-127e removed them, a later `pip freeze` re-added them and production
started resetting connections mid-request / Cloudflare 520).

forecast_agent.py lazy-imports chronos/torch and degrades gracefully, so
preview keeps them installed locally WITHOUT listing them for deployment.
"""
import os

_REQ = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "requirements.txt")

FORBIDDEN_PREFIXES = (
    "torch==", "torch>=",
    "transformers==", "transformers>=",
    "accelerate==",
    "chronos-forecasting==",
    "nvidia-",
)


def test_no_heavy_ml_deps_in_requirements():
    with open(_REQ) as f:
        lines = [ln.strip() for ln in f if ln.strip()
                 and not ln.strip().startswith("#")]
    offenders = [ln for ln in lines
                 if any(ln.lower().startswith(p) for p in FORBIDDEN_PREFIXES)]
    assert offenders == [], (
        f"Heavy ML deps back in requirements.txt: {offenders}. "
        "They OOM the 1Gi production pod (Cloudflare 520 on every request). "
        "Never regenerate requirements.txt with a blind `pip freeze` — "
        "preview has torch installed locally for forecast_agent.")


def test_no_pytorch_extra_index():
    with open(_REQ) as f:
        content = f.read()
    assert "download.pytorch.org" not in content, (
        "pytorch extra-index-url present — only needed when torch is listed, "
        "which is forbidden for deployment.")


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
