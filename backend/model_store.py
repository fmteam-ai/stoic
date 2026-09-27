"""Content-addressed, shared model artifact store (audit round 13 P1-01).

Bytes live in Mongo GridFS (bucket `model_artifacts`, filename = sha256), shared
by the API and every trading-worker container and persisted with the Mongo
volume. Provenance lives in `model_artifact_meta` {_id: digest}. Activation is a
DB pointer change (ml_ensembles.production.digest) — never a local file move.
Every inference instance fetches + hash-verifies the exact active digest into a
local content-addressed cache (MODEL_DIR/_cache/sha256/<digest>.joblib).
"""
import hashlib
import os
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorGridFSBucket

BUCKET = "model_artifacts"


def cache_path(model_dir: Path, digest: str) -> Path:
    return model_dir / "_cache" / "sha256" / f"{digest}.joblib"


def instance_id() -> str:
    return os.environ.get("STOIC_INSTANCE_ID") or os.environ.get("HOSTNAME") or "local"


async def put(db, data: bytes, digest: str, user_id: str, provenance: dict) -> str:
    """Idempotent upload: same digest ⇒ same immutable object."""
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("artifact digest mismatch on upload")
    if await db.fs_model_artifacts_index.find_one({"_id": digest}) is None:
        bucket = AsyncIOMotorGridFSBucket(db, bucket_name=BUCKET)
        await bucket.upload_from_stream(digest, data, metadata={"digest": digest, "user_id": user_id})
        await db.fs_model_artifacts_index.update_one({"_id": digest}, {"$setOnInsert": {"user_id": user_id,
                                                                                         "bytes": len(data)}}, upsert=True)
    await db.model_artifact_meta.update_one({"_id": digest}, {"$set": {"user_id": user_id, "provenance": provenance}},
                                            upsert=True)
    return digest


async def exists(db, digest: str) -> bool:
    return await db.fs_model_artifacts_index.find_one({"_id": digest}) is not None


async def provenance(db, digest: str) -> dict | None:
    doc = await db.model_artifact_meta.find_one({"_id": digest})
    return (doc or {}).get("provenance")


async def fetch(db, digest: str, model_dir: Path) -> Path | None:
    """Local verified copy of the exact digest (downloads once), or None."""
    p = cache_path(model_dir, digest)
    if p.exists() and _sha(p) == digest:
        return p
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name=BUCKET)
    try:
        stream = await bucket.open_download_stream_by_name(digest)
        data = await stream.read()
    except Exception:  # noqa: BLE001 — missing object / store unavailable ⇒ caller withholds the vote
        return None
    if hashlib.sha256(data).hexdigest() != digest:
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return p


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
