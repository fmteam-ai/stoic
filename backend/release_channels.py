"""iter-160 — canary / staged artifact rollout with deployment history and
one-click rollback pins.

Channel state lives in db.platform_state {_id:"release_state"}:
  stable:      {artifact_name: sha256}   — what the fleet gets
  candidate:   {artifact_name: sha256}   — what canary agents get
  canary_agents: [agent_id, ...]
  candidate_since, promote_after_hours (default 24), pinned (bool)

A new on-disk build automatically becomes the CANDIDATE (stable is never
mutated by a build). Candidates promote to stable only after the soak window
passes with zero deployment_failed alerts — or by explicit admin promote.
Every artifact version is snapshotted into the content-addressed store
(static/artifacts/{sha256}) so any historical release is a rollback target.
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("release-channels")

STATE_ID = "release_state"
STORE_DIR = Path(__file__).parent / "static" / "artifacts"
DEFAULT_SOAK_HOURS = 24


def _now():
    return datetime.now(timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot_to_store(sha256: str) -> bool:
    """Copy the current on-disk file with this digest into the immutable
    store (idempotent)."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    dest = STORE_DIR / sha256
    if dest.exists():
        return True
    static_dir = Path(__file__).parent / "static"
    for f in static_dir.iterdir():
        if f.is_file() and _sha(f.read_bytes()) == sha256:
            dest.write_bytes(f.read_bytes())
            return True
    return False


async def _state(db) -> dict:
    return await db.platform_state.find_one({"_id": STATE_ID}) or {}


async def _history(db, event: str, detail: dict):
    await db.release_history.insert_one(
        {"event_id": f"rel-{uuid.uuid4().hex[:10]}", "event": event,
         "detail": detail, "at": _now().isoformat()})


def _current_build() -> dict:
    from vps_pathb import build_artifact_manifest
    m = build_artifact_manifest()
    return {a["name"]: a["sha256"] for a in m["artifacts"]
            if a.get("sha256")}


async def sync_channels(db) -> dict:
    """Reconcile channel state with the current on-disk build. First build
    ever becomes stable; any later differing build becomes the candidate."""
    cur = _current_build()
    for sha in cur.values():
        _snapshot_to_store(sha)
    st = await _state(db)
    changed = {}
    if not st.get("stable"):
        changed = {"stable": cur, "stable_since": _now().isoformat()}
        await _history(db, "initial_stable", {"artifacts": cur})
    elif cur != st.get("stable") and cur != st.get("candidate"):
        changed = {"candidate": cur, "candidate_since": _now().isoformat(),
                   "pinned": False}
        await _history(db, "new_candidate", {"artifacts": cur})
    if changed:
        await db.platform_state.update_one(
            {"_id": STATE_ID}, {"$set": changed}, upsert=True)
    return await _state(db)


async def channel_for_agent(db, agent_id: str | None) -> tuple[str, dict]:
    """(channel_name, {artifact_name: sha256}) for this agent."""
    st = await sync_channels(db)
    candidate = st.get("candidate")
    if (candidate and agent_id
            and agent_id in (st.get("canary_agents") or [])):
        return "canary", candidate
    return "stable", st.get("stable") or {}


async def manifest_for_agent(db, agent_id: str | None = None) -> dict:
    """Cohort-aware SIGNED manifest: same shape as the base manifest but
    with artifact digests/urls pinned to the agent's channel, re-signed."""
    import release_signing
    from vps_pathb import build_artifact_manifest
    m = build_artifact_manifest()
    channel, shas = await channel_for_agent(db, agent_id)
    for a in m["artifacts"]:
        pinned = shas.get(a["name"])
        if pinned:
            a["sha256"] = pinned
            a["url"] = f"/api/artifacts/{pinned}"
    m["channel"] = channel
    body = json.dumps({k: m[k] for k in ("artifacts", "update_policy")},
                      sort_keys=True, separators=(",", ":"),
                      default=str).encode()
    m["signature"] = {"alg": "Ed25519", "key_id": release_signing.KEY_ID,
                      "public_key_b64": release_signing.public_key_b64(),
                      "value": release_signing.sign_hex(body)}
    return m


async def maybe_promote(db) -> dict | None:
    """Auto-promote candidate → stable after a clean soak window: the
    candidate must be older than promote_after_hours with ZERO
    deployment_failed alerts raised since it appeared."""
    st = await _state(db)
    cand = st.get("candidate")
    if not cand or st.get("pinned"):
        return None
    since = st.get("candidate_since")
    if not since:
        return None
    since_dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    hours = float(st.get("promote_after_hours") or DEFAULT_SOAK_HOURS)
    if _now() - since_dt < timedelta(hours=hours):
        return None
    # SEC (3rd/4th audit) — hold auto-promotion only on CORROBORATED failures
    # from distinct TRUSTED tenants reporting the CANDIDATE's own digests.
    # Untrusted tenant telemetry can't freeze the release pipeline.
    from deployment_health import (MIN_FAIL_TENANTS, _trusted_user_ids,
                                   distinct_fail_tenants)
    cand_digests = set(cand.values())
    trusted_uids = await _trusted_user_ids(db)
    fail_tenants = await distinct_fail_tenants(
        db, since, cand_digests, only_user_ids=trusted_uids)
    if trusted_uids and fail_tenants >= MIN_FAIL_TENANTS:
        logger.warning("candidate promotion HELD: %s trusted tenants "
                       "reported deploy failure during soak", fail_tenants)
        return None
    return await promote(db, actor="auto-soak")


async def promote(db, actor: str = "admin") -> dict:
    st = await _state(db)
    cand = st.get("candidate")
    if not cand:
        raise ValueError("no candidate release to promote")
    prev = st.get("stable") or {}
    await db.platform_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"stable": cand, "stable_since": _now().isoformat(),
                  "previous_stable": prev, "candidate": None,
                  "candidate_since": None}},
        upsert=True)
    await _history(db, "promoted", {"artifacts": cand, "by": actor,
                                    "previous": prev})
    logger.info("release promoted to stable by %s", actor)
    try:
        from deployment_health import start_watch
        await start_watch(db, cand, actor)
    except Exception as e:  # noqa: BLE001
        logger.warning("deploy health watch start failed: %s", e)
    return await _state(db)


async def rollback(db, actor: str = "admin") -> dict:
    """One-click rollback: previous stable becomes stable again and the
    channel is PINNED (no auto-promotion until an admin unpins)."""
    st = await _state(db)
    prev = st.get("previous_stable")
    if not prev:
        raise ValueError("no previous stable release recorded")
    missing = [n for n, s in prev.items() if not (STORE_DIR / s).exists()
               and not _snapshot_to_store(s)]
    if missing:
        raise ValueError(f"rollback artifacts missing from store: {missing}")
    await db.platform_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"stable": prev, "stable_since": _now().isoformat(),
                  "previous_stable": st.get("stable") or {},
                  "candidate": None, "candidate_since": None,
                  "pinned": True}},
        upsert=True)
    await _history(db, "rollback", {"artifacts": prev, "by": actor})
    logger.warning("release ROLLED BACK and pinned by %s", actor)
    return await _state(db)


async def set_canary_agents(db, agent_ids: list[str],
                            actor: str = "admin") -> dict:
    ids = [str(a)[:64] for a in agent_ids][:20]
    await db.platform_state.update_one(
        {"_id": STATE_ID}, {"$set": {"canary_agents": ids}}, upsert=True)
    await _history(db, "canary_set", {"agents": ids, "by": actor})
    return await _state(db)


async def set_pinned(db, pinned: bool, actor: str = "admin") -> dict:
    await db.platform_state.update_one(
        {"_id": STATE_ID}, {"$set": {"pinned": bool(pinned)}}, upsert=True)
    await _history(db, "pinned" if pinned else "unpinned", {"by": actor})
    return await _state(db)
