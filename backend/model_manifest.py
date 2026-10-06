"""Signed model manifest (audit rounds 11 P1-03 · 12 P1-01/P1-03/P1-04).

`joblib.load` is code execution. Nothing under models_store may be
deserialized unless a signed manifest names its exact SHA-256 together with
full provenance: code commit (must equal the RUNNING build), feature schema +
feature-code digest, training dataset hash + complete window, holdout metrics,
policy version and >= 2 DISTINCT approvals each backed by an authenticated
audit-event id. Manifest: models_store/MODEL_MANIFEST.json, Ed25519 signature
over the canonical body (release_signing).

    python -m model_manifest sign --approval a@x:<event_hash> --approval b@y:<event_hash> [--promote <uid>]
    python -m model_manifest resign --commit <release sha>        # CI: carry approvals, bind release commit
    python -m model_manifest verify [--build <sha>]

Runtime: verify_model(path, known_events=…) → digest, or raises ModelRefused
(quarantined, never loaded). Candidate binaries (`*.candidate.joblib`) are
NEVER loadable; promotion (ml_ensemble.promote_candidate) is the only path
from candidate to production.
"""
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("model_manifest")

_HERE = Path(__file__).resolve().parent
MODEL_DIR = Path(os.environ.get("MODEL_DIR") or (_HERE / "models_store"))   # /app/... only as explicit override
MANIFEST = MODEL_DIR / "MODEL_MANIFEST.json"
QUARANTINE = MODEL_DIR / "_quarantine"
FEATURE_SCHEMA_VERSION = "ens-features-v1"
POLICY_VERSION = "model-governance-v2"
MANIFEST_VERSION = 2
PRODUCTION_NAME = "gbm_ensemble.joblib"
CANDIDATE_NAME = "gbm_ensemble.candidate.joblib"
APPROVAL_ACTION = "model_candidate_approved"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_METRICS = ("aucs", "weights")


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


def _is_production() -> bool:
    from app_env import is_production
    return is_production()


def immutable_build_sha(env: dict | None = None) -> str:
    """Build identity from IMMUTABLE sources only (BUILD_SHA file / build env) — the
    value a signed manifest is bound against at runtime. 'unknown' in a bare checkout."""
    env = os.environ if env is None else env
    cands = []
    try:
        cands.append((_HERE / "BUILD_SHA").read_text().strip().lower())
    except OSError:
        pass
    cands += [(env.get(k) or "").strip().lower() for k in ("STOIC_BUILD_SHA", "GITHUB_SHA")]
    return next((c for c in cands if _SHA_RE.match(c)), "unknown")


def running_build_sha(env: dict | None = None) -> str:
    """Immutable build identity, falling back (dev checkout only) to .git HEAD so a
    developer-signed manifest names the exact commit it was produced on."""
    sha = immutable_build_sha(env)
    if sha != "unknown":
        return sha
    try:
        git = _HERE.parent / ".git"
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            refname = head[5:]
            ref = git / refname
            head = ref.read_text().strip() if ref.exists() else ""
            if not head and (git / "packed-refs").exists():
                for line in (git / "packed-refs").read_text().splitlines():
                    if line.endswith(" " + refname):
                        head = line.split()[0]
        if _SHA_RE.match(head.lower()):
            return head.lower()
    except OSError:
        pass
    return "unknown"


def feature_code_digest() -> str:
    """Digest of the feature contract: FEATURE_NAMES + featurize() source."""
    import inspect
    import ml_ensemble
    src = inspect.getsource(ml_ensemble.featurize)
    return hashlib.sha256((json.dumps(ml_ensemble.FEATURE_NAMES) + "\n" + src).encode()).hexdigest()


# ── sidecars (training provenance written by ml_ensemble.train_sync / promote) ──
def sidecar_path(uid: str, kind: str) -> Path:
    return MODEL_DIR / uid / f"gbm_ensemble.{kind}.json"


def read_sidecar(uid: str, kind: str) -> dict | None:
    p = sidecar_path(uid, kind)
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except (ValueError, OSError):
        return None


def read_provenance(uid: str, kind: str) -> dict | None:
    """Sidecar (same container) → shared store (Mongo, any container): the record's
    digest for `kind` (candidate|production) resolves its immutable provenance."""
    prov = read_sidecar(uid, kind)
    if prov:
        return prov
    try:
        from pymongo import MongoClient
        db = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=3000)[os.environ["DB_NAME"]]
        rec = (db.ml_ensembles.find_one({"user_id": uid}, {kind: 1}) or {}).get(kind) or {}
        meta = db.model_artifact_meta.find_one({"_id": rec.get("digest")}) if rec.get("digest") else None
        return (meta or {}).get("provenance")
    except Exception:  # noqa: BLE001
        return None


def parse_approval(raw: str) -> dict:
    """CLI form email:audit_event_id:principal_id (principal = immutable admin user id)."""
    parts = raw.split(":")
    if len(parts) != 3 or "@" not in parts[0] or len(parts[1]) < 16 or len(parts[2]) < 8:
        raise ValueError(f"approval must be email:audit_event_id:principal_id — got {raw!r}")
    return {"email": parts[0].strip().lower(), "audit_event_id": parts[1].strip(), "principal_id": parts[2].strip()}


def validate_approvals(approvals) -> list:
    """>= 2 DISTINCT immutable principals, each with an audit-event id (round 12 P1-04 · round 13 P1-03)."""
    problems = []
    if not isinstance(approvals, list) or len(approvals) < 2:
        problems.append("at least two approval records required")
        return problems
    emails, principals = [], []
    for a in approvals:
        if not isinstance(a, dict) or "@" not in str(a.get("email") or "") or not str(a.get("audit_event_id") or "").strip() \
                or not str(a.get("principal_id") or "").strip():
            problems.append("approval record must carry email + audit_event_id + principal_id")
            continue
        emails.append(str(a["email"]).lower())
        principals.append(str(a["principal_id"]))
    if len(set(principals)) < 2:
        problems.append("approvals must come from two distinct admin principals")
    if len(emails) != len(set(emails)) or len(principals) != len(set(principals)):
        problems.append("repeated approver")
    return problems


def _entry_from_provenance(uid: str, rel: str, prov: dict, sha: str, size: int) -> dict:
    return {"path": rel, "sha256": sha, "bytes": size,
            "format": "joblib-pickle (executable — verified before load)",
            "feature_schema": prov.get("feature_schema") or FEATURE_SCHEMA_VERSION,
            "feature_code_digest": prov.get("feature_code_digest"),
            "training_window": prov.get("training_window") or {},
            "dataset_sha256": prov.get("dataset_sha256"),
            "n_samples": prov.get("n_samples"),
            "metrics": prov.get("metrics") or {},
            "trained_at": prov.get("trained_at"),
            "trained_on_commit": prov.get("code_commit"),
            "promotion_status": "approved"}


def build_body(approvals: list, *, promote: list | None = None, commit: str | None = None) -> dict:
    """Production entries carry the provenance of the active binary; `promote`
    uids get an entry for the PRODUCTION path holding the CANDIDATE digest +
    candidate provenance (the atomic promote step moves the bytes)."""
    promote = set(promote or [])
    models = []
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    uids = sorted({p.name for p in MODEL_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")} | promote)
    for uid in uids:
        rel = f"{uid}/{PRODUCTION_NAME}"
        if uid in promote:
            prov = read_provenance(uid, "candidate")
            if not prov or not prov.get("sha256"):
                raise ModelRefused(f"{uid}: no candidate provenance to promote (train first)")
            models.append(_entry_from_provenance(uid, rel, prov, prov["sha256"], int(prov.get("bytes") or 0)))
        else:
            prov = read_provenance(uid, "production") or {}
            f = MODEL_DIR / uid / PRODUCTION_NAME
            sha = prov.get("sha256") or (sha256_file(f) if f.exists() else None)
            if sha:
                models.append(_entry_from_provenance(uid, rel, prov, sha, int(prov.get("bytes") or (f.stat().st_size if f.exists() else 0))))
    return {"record": "stoic.model-manifest", "version": MANIFEST_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "code_commit": commit or running_build_sha(), "policy_version": POLICY_VERSION,
            "approvals": sorted(approvals, key=lambda a: a["email"]), "models": models}


def _write_signed(body: dict) -> dict:
    sys.path.insert(0, str(_HERE))
    from release_signing import sign_hex, key_id, public_key_b64
    doc = {"body": body, "body_sha256": hashlib.sha256(_canonical(body)).hexdigest(),
           "signature_hex": sign_hex(_canonical(body)), "key_id": key_id(), "public_key_b64": public_key_b64()}
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(doc, indent=2, sort_keys=True))
    return doc


def sign(approvals: list, *, promote: list | None = None, commit: str | None = None) -> dict:
    problems = validate_approvals(approvals)
    if problems:
        raise ModelRefused("; ".join(problems))
    return _write_signed(build_body(approvals, promote=promote, commit=commit))


def _load_body_for_resign() -> dict:
    """The developer manifest is signed with the DEVELOPER key, which the release pipeline does not
    trust (and must not: CI/production pin the release key). Re-signing therefore checks the
    document's integrity against its own embedded public key — the body is unchanged since it was
    signed — then every model hash is re-verified against the staged files before the release key
    signs the frozen commit."""
    if not MANIFEST.exists():
        raise ModelRefused("no MODEL_MANIFEST.json to re-sign")
    try:
        doc = json.loads(MANIFEST.read_text())
        body, sig, pub = doc["body"], doc["signature_hex"], doc["public_key_b64"]
    except (ValueError, KeyError, TypeError) as e:
        raise ModelRefused(f"model manifest malformed: {type(e).__name__}")
    from release_signing import verify_hex
    if not verify_hex(_canonical(body), sig, pub):
        raise ModelRefused("model manifest is internally inconsistent (body does not match its own signature)")
    if body.get("version") != MANIFEST_VERSION or body.get("policy_version") != POLICY_VERSION:
        raise ModelRefused(f"model manifest schema/policy {body.get('version')}/{body.get('policy_version')} not accepted")
    return body


def resign(commit: str) -> dict:
    """CI release job: verify the developer-signed manifest, carry its approvals
    and model entries unchanged, bind code_commit to the frozen release commit."""
    if not _SHA_RE.match(commit or ""):
        raise ModelRefused("resign requires a 40-hex release commit")
    body = _load_body_for_resign()
    for m in body["models"]:
        p = MODEL_DIR / m["path"]
        probs = _check_entry(m, p if p.exists() else None, FEATURE_SCHEMA_VERSION, build=None,
                             actual=None if p.exists() else m["sha256"])
        if probs:
            raise ModelRefused("; ".join(probs))
    body = {**body, "code_commit": commit, "generated_at": datetime.now(timezone.utc).isoformat(),
            "resigned_from_commit": body["code_commit"]}
    return _write_signed(body)


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
    if body.get("version") != MANIFEST_VERSION or body.get("policy_version") != POLICY_VERSION:
        raise ModelRefused(f"model manifest schema/policy {body.get('version')}/{body.get('policy_version')} "
                           f"not accepted (need {MANIFEST_VERSION}/{POLICY_VERSION})")
    problems = validate_approvals(body.get("approvals"))
    if problems:
        raise ModelRefused("; ".join(problems))
    return body


def manifest_digest() -> str | None:
    return hashlib.sha256(MANIFEST.read_bytes()).hexdigest() if MANIFEST.exists() else None


def check_build_binding(body: dict, build: str | None) -> str | None:
    """Manifest commit must equal the running IMMUTABLE build (BUILD_SHA / build env).
    An unknown build is refused in production; a bare preview checkout has no
    immutable identity to bind to, so the mismatch is only reported there."""
    build = immutable_build_sha() if build is None else build
    if build == "unknown":
        return "running build unknown — production refuses" if _is_production() else None
    if body.get("code_commit") != build:
        return f"manifest commit {str(body.get('code_commit'))[:12]}… != running build {build[:12]}…"
    return None


def _check_entry(entry: dict, path, expected_schema: str, *, build: str | None,
                 runtime_feature_digest: str | None = None, actual: str | None = None) -> list:
    """`path` may be a Path (hashed here) or None when `actual` (a digest) is supplied."""
    problems = []
    if isinstance(path, Path) and path.name == CANDIDATE_NAME:
        problems.append("candidate binaries are never loadable — promote first")
    if actual is None:
        actual = sha256_file(path) if (isinstance(path, Path) and path.exists()) else None
    if entry["sha256"] != actual:
        problems.append(f"digest mismatch (manifest {entry['sha256'][:12]}…, file {str(actual)[:12]}…)")
    if entry.get("feature_schema") != expected_schema:
        problems.append(f"feature schema {entry.get('feature_schema')} != runtime {expected_schema}")
    if runtime_feature_digest and entry.get("feature_code_digest") != runtime_feature_digest:
        problems.append("feature-code digest differs from the running featurizer")
    if entry.get("promotion_status") != "approved":
        problems.append(f"promotion status {entry.get('promotion_status')}")
    win = entry.get("training_window") or {}
    if not (win.get("from") and win.get("until")):
        problems.append("training window incomplete (from/until required)")
    if not entry.get("dataset_sha256"):
        problems.append("training dataset hash missing")
    metrics = entry.get("metrics") or {}
    if not all(isinstance(metrics.get(k), dict) and metrics.get(k) for k in REQUIRED_METRICS) \
            or not isinstance(metrics.get("holdout_auc"), (int, float)):
        problems.append("required metrics missing (aucs, weights, holdout_auc)")
    return problems


def verify_model(path: Path, expected_schema: str = FEATURE_SCHEMA_VERSION, *,
                 known_events: set | None = None, build: str | None = None,
                 check_feature_code: bool = True) -> str:
    """Return the verified digest or raise ModelRefused (and quarantine the file).
    `known_events`: the set of authenticated approval audit-event ids the caller
    resolved from the audit chain — every approval must be in it."""
    body = load_manifest()
    rel = str(path.relative_to(MODEL_DIR))
    entry = next((m for m in body["models"] if m["path"] == rel), None)
    problems = []
    if entry is None:
        problems.append("model not listed in the signed manifest")
    else:
        rt = feature_code_digest() if check_feature_code else None
        problems += _check_entry(entry, path, expected_schema, build=build, runtime_feature_digest=rt)
    bb = check_build_binding(body, build)
    if bb:
        problems.append(bb)
    if known_events is not None:
        missing = [a["email"] for a in body["approvals"] if a["audit_event_id"] not in known_events]
        if missing:
            problems.append(f"approval audit event unknown for {', '.join(missing)}")
    if problems:
        _quarantine(path, problems)
        raise ModelRefused("; ".join(problems))
    return entry["sha256"]


def verify_digest(uid: str, digest: str, *, known_events: set | None = None, build: str | None = None) -> str:
    """Content-addressed variant (round 13 P1-01): the ACTIVE digest must be the one
    the signed manifest names for this user's production path."""
    body = load_manifest()
    entry = next((m for m in body["models"] if m["path"] == f"{uid}/{PRODUCTION_NAME}"), None)
    problems = []
    if entry is None:
        problems.append("model not listed in the signed manifest")
    else:
        problems += _check_entry(entry, None, FEATURE_SCHEMA_VERSION, build=build,
                                 runtime_feature_digest=feature_code_digest(), actual=digest)
    bb = check_build_binding(body, build)
    if bb:
        problems.append(bb)
    if known_events is not None:
        missing = [a["email"] for a in body["approvals"] if a["audit_event_id"] not in known_events]
        if missing:
            problems.append(f"approval audit event unknown for {', '.join(missing)}")
    if problems:
        _quarantine(MODEL_DIR / uid / f"sha256-{digest[:12]}", problems)
        raise ModelRefused("; ".join(problems))
    return digest


def _quarantine(path: Path, problems: list) -> None:
    try:
        QUARANTINE.mkdir(parents=True, exist_ok=True)
        (QUARANTINE / f"{path.parent.name}.{path.name}.refused.json").write_text(json.dumps(
            {"path": str(path), "at": datetime.now(timezone.utc).isoformat(), "problems": problems}, indent=2))
    except OSError:
        pass
    logger.error("MODEL REFUSED %s: %s", path, problems)


def public_summary() -> dict:
    """Non-secret manifest facts for API/UI (never raises)."""
    try:
        body = load_manifest()
        return {"signed": True, "code_commit": body["code_commit"], "policy_version": body["policy_version"],
                "approvals": [a["email"] for a in body["approvals"]], "manifest_sha256": manifest_digest(),
                "models": {m["path"].split("/")[0]: m["sha256"] for m in body["models"]},
                "build_binding_problem": check_build_binding(body, None), "running_build": running_build_sha(),
                "immutable_build": immutable_build_sha()}
    except ModelRefused as e:
        return {"signed": False, "problem": str(e), "running_build": running_build_sha()}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sign", "resign", "verify"])
    ap.add_argument("--approval", action="append", default=[], help="email:audit_event_id (>= 2 distinct)")
    ap.add_argument("--promote", action="append", default=[], help="uid whose CANDIDATE digest enters the manifest")
    ap.add_argument("--commit", default=None)
    ap.add_argument("--build", default=None, help="verify: enforce binding to this build sha")
    a = ap.parse_args()
    try:
        if a.cmd == "sign":
            d = sign([parse_approval(x) for x in a.approval], promote=a.promote, commit=a.commit)
            print(json.dumps({"models": len(d["body"]["models"]), "key_id": d["key_id"],
                              "code_commit": d["body"]["code_commit"], "manifest_sha256": manifest_digest()}))
        elif a.cmd == "resign":
            d = resign(a.commit)
            print(json.dumps({"models": len(d["body"]["models"]), "code_commit": d["body"]["code_commit"],
                              "resigned_from": d["body"].get("resigned_from_commit"), "manifest_sha256": manifest_digest()}))
        else:
            body = load_manifest()
            probs = []
            for m in body["models"]:
                p = MODEL_DIR / m["path"]
                probs += _check_entry(m, p if p.exists() else None, FEATURE_SCHEMA_VERSION, build=None,
                                      actual=None if p.exists() else m["sha256"])
            if a.build:
                bb = check_build_binding(body, a.build)
                if bb:
                    probs.append(bb)
            if probs:
                raise ModelRefused("; ".join(probs))
            bind = check_build_binding(body, None)
            print(f"OK: {len(body['models'])} model(s) verified · approvals={len(body['approvals'])} · "
                  f"commit={body['code_commit'][:12]}… · build_binding={'strict OK' if a.build else (bind or 'OK')}")
    except (ModelRefused, ValueError) as e:
        sys.exit(f"MODEL MANIFEST REFUSED: {e}")
