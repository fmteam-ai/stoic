"""iter-171 (#8) — externally-anchorable audit log.

The admin audit log is already tamper-evident (hash-chained, audit_chain.py).
This adds an *anchor*: the current chain head (seq + entry_hash) is Ed25519-
signed with the release key and persisted to an append-only `audit_anchors`
collection (and, when configured, pushed to external immutable storage). A
signed anchor is portable proof of the chain head at a point in time — even a
full DB rewrite can't forge a head that matches a previously-exported anchor.
"""
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("audit-anchor")
CHAIN_COLLECTION = "admin_audit_log"


def _now():
    return datetime.now(timezone.utc)


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)


async def _chain_head(db):
    return await db[CHAIN_COLLECTION].find_one(
        {"entry_hash": {"$exists": True}}, sort=[("seq", -1)])


async def create_anchor(db) -> dict | None:
    """Sign and persist the current audit-chain head. Idempotent per head:
    re-anchoring the same seq is skipped."""
    head = await _chain_head(db)
    if not head:
        return None
    existing = await db.audit_anchors.find_one({"seq": head["seq"]})
    if existing:
        existing.pop("_id", None)
        return existing
    body = {"seq": head["seq"], "entry_hash": head["entry_hash"],
            "collection": CHAIN_COLLECTION, "anchored_at": _now().isoformat()}
    from release_signing import KEY_ID, public_key_b64, sign_hex
    doc = {**body, "signature": sign_hex(_canon(body).encode()),
           "key_id": KEY_ID, "public_key_b64": public_key_b64()}
    await db.audit_anchors.insert_one(dict(doc))
    await _push_external(doc)
    logger.info("audit anchor created at seq=%s", head["seq"])
    doc.pop("_id", None)
    return doc


async def _push_external(doc: dict) -> None:
    """Best-effort push to external immutable storage. If OBJECT storage isn't
    configured we mirror to a local append-only file so the anchor survives a
    DB wipe; production should point this at S3 Object-Lock / WORM."""
    try:
        d = os.environ.get("AUDIT_ANCHOR_DIR", "/app/backend/audit_anchors")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"anchor_{doc['seq']:012d}.json"), "w") as f:
            f.write(_canon(doc))
    except Exception as e:  # noqa: BLE001
        logger.warning("external anchor push failed: %s", e)


async def verify_latest(db) -> dict:
    """Verify the newest anchor's signature AND that the live chain head still
    covers it (chain not truncated below the anchored seq)."""
    anchor = await db.audit_anchors.find_one(sort=[("seq", -1)])
    if not anchor:
        return {"ok": True, "anchored": False,
                "detail": "no anchors yet"}
    body = {k: anchor[k] for k in
            ("seq", "entry_hash", "collection", "anchored_at")}
    from release_signing import verify_hex
    sig_ok = verify_hex(_canon(body).encode(), anchor["signature"],
                        anchor.get("public_key_b64"))
    head = await _chain_head(db)
    head_seq = head["seq"] if head else 0
    covered = head_seq >= anchor["seq"]
    anchored_entry = await db[CHAIN_COLLECTION].find_one(
        {"seq": anchor["seq"]})
    entry_matches = bool(anchored_entry and
                         anchored_entry.get("entry_hash") == anchor["entry_hash"])
    return {"ok": bool(sig_ok and covered and entry_matches),
            "anchored": True, "anchor_seq": anchor["seq"],
            "current_head_seq": head_seq,
            "signature_valid": bool(sig_ok),
            "chain_covers_anchor": covered,
            "anchored_entry_intact": entry_matches}
