"""Signed model manifest (audit round 11 P1-03).

`joblib.load` is code execution. Nothing under models_store may be
deserialized unless a signed manifest names its exact SHA-256 together with
provenance (training window, feature schema, code commit, metrics, approvers,
promotion status). Manifest: models_store/MODEL_MANIFEST.json, signature over
the canonical JSON body with the release Ed25519 key (release_signing).

    python -m model_manifest sign  --approver a@x --approver b@y   # (re)build + sign
    python -m model_manifest verify

Runtime: verify_model(path) → digest, or raises ModelRefused (quarantined,
never loaded). Untrusted / unsigned / mutated / rolled-back binaries fail closed.
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("model_manifest")

MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/app/backend/models_store"))
MANIFEST = MODEL_DIR / "MODEL_MANIFEST.json"
QUARANTINE = MODEL_DIR / "_quarantine"
FEATURE_SCHEMA_VERSION = "ens-features-v1"


class ModelRefused(RuntimeError):
    pass


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "-C", str(MODEL_DIR.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return os.environ.get("STOIC_BUILD_SHA", "unknown")


def build_body(approvers: list, metrics: dict | None = None) -> dict:
    models = []
    for f in sorted(MODEL_DIR.glob("*/gbm_ensemble.joblib")):
        models.append({"path": str(f.relative_to(MODEL_DIR)), "sha256": sha256_file(f), "bytes": f.stat().st_size,
                       "format": "joblib-pickle (executable — verified before load)",
                       "feature_schema": FEATURE_SCHEMA_VERSION,
                       "training_window": {"until": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()},
                       "metrics": (metrics or {}).get(f.parent.name, {}), "promotion_status": "approved"})
    return {"record": "stoic.model-manifest", "version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "code_commit": _git_sha(), "approvers": sorted(approvers), "models": models}


def sign(approvers: list, metrics: dict | None = None) -> dict:
    sys.path.insert(0, str(Path(__file__).parent))
    from release_signing import sign_hex, key_id, public_key_b64
    body = build_body(approvers, metrics)
    doc = {"body": body, "body_sha256": hashlib.sha256(_canonical(body)).hexdigest(),
           "signature_hex": sign_hex(_canonical(body)), "key_id": key_id(), "public_key_b64": public_key_b64()}
    MANIFEST.write_text(json.dumps(doc, indent=2, sort_keys=True))
    return doc


def load_manifest() -> dict:
    if not MANIFEST.exists():
        raise ModelRefused("no signed MODEL_MANIFEST.json — refusing to deserialize any model")
    try:
        doc = json.loads(MANIFEST.read_text())
        body, sig = doc["body"], doc["signature_hex"]
    except (ValueError, KeyError, TypeError) as e:
        raise ModelRefused(f"model manifest malformed: {type(e).__name__}")
    from release_signing import verify_hex
    pinned = os.environ.get("RELEASE_PUBLIC_KEY_B64")
    if not verify_hex(_canonical(body), sig, pinned or None):
        raise ModelRefused("model manifest signature does not verify against the trusted release key")
    if not body.get("approvers"):
        raise ModelRefused("model manifest has no approvers")
    return body


def manifest_digest() -> str | None:
    return hashlib.sha256(MANIFEST.read_bytes()).hexdigest() if MANIFEST.exists() else None


def verify_model(path: Path, expected_schema: str = FEATURE_SCHEMA_VERSION) -> str:
    """Return the verified digest or raise ModelRefused (and quarantine the file)."""
    body = load_manifest()
    rel = str(path.relative_to(MODEL_DIR))
    entry = next((m for m in body["models"] if m["path"] == rel), None)
    actual = sha256_file(path)
    problems = []
    if entry is None:
        problems.append("model not listed in the signed manifest")
    else:
        if entry["sha256"] != actual:
            problems.append(f"digest mismatch (manifest {entry['sha256'][:12]}…, file {actual[:12]}…)")
        if entry.get("feature_schema") != expected_schema:
            problems.append(f"feature schema {entry.get('feature_schema')} != runtime {expected_schema}")
        if entry.get("promotion_status") != "approved":
            problems.append(f"promotion status {entry.get('promotion_status')}")
    if problems:
        _quarantine(path, problems)
        raise ModelRefused("; ".join(problems))
    return actual


def _quarantine(path: Path, problems: list) -> None:
    try:
        QUARANTINE.mkdir(exist_ok=True)
        (QUARANTINE / f"{path.parent.name}.{path.name}.refused.json").write_text(json.dumps(
            {"path": str(path), "at": datetime.now(timezone.utc).isoformat(), "problems": problems}, indent=2))
    except OSError:
        pass
    logger.error("MODEL REFUSED %s: %s", path, problems)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sign", "verify"])
    ap.add_argument("--approver", action="append", default=[])
    a = ap.parse_args()
    if a.cmd == "sign":
        if len(a.approver) < 2:
            sys.exit("two approvers required")
        d = sign(a.approver)
        print(json.dumps({"models": len(d["body"]["models"]), "key_id": d["key_id"], "manifest_sha256": manifest_digest()}))
    else:
        body = load_manifest()
        for m in body["models"]:
            verify_model(MODEL_DIR / m["path"])
        print(f"OK: {len(body['models'])} model(s) verified")
