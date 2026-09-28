"""Deploy Watchdog — an admin arms a watch on a public deployment URL; a
background loop polls {target}/api/health until the expected build_sha is
live (→ "live" e-mail) or the watch times out (→ "stalled" e-mail).

Collection `deploy_watch`: one row per watch (status watching|live|stalled|cancelled).
"""
import asyncio
import html
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

logger = logging.getLogger("deploy_watch")

DEFAULT_ALLOWED_HOSTS = "www.stoicaibot.com,stoicaibot.com"
MAX_TIMEOUT_H = 72
PROVIDER_IDEMPOTENCY_RETENTION = timedelta(hours=23)   # Resend keeps Idempotency-Key for 24 h
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _now():
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def allowed_hosts(env=None) -> set[str]:
    raw = (env or os.environ).get("DEPLOY_WATCH_ALLOWED_HOSTS") or DEFAULT_ALLOWED_HOSTS
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def validate_target(url: str) -> str:
    """https only, host on the allow-list, no credentials/query — returns the normalised origin."""
    p = urlparse((url or "").strip())
    if p.scheme != "https" or not p.hostname:
        raise ValueError("target_url must be an https URL")
    if p.username or p.password or p.query or p.fragment:
        raise ValueError("target_url must be a bare origin")
    if p.hostname.lower() not in allowed_hosts():
        raise ValueError(f"host {p.hostname!r} is not in DEPLOY_WATCH_ALLOWED_HOSTS")
    port = f":{p.port}" if p.port and p.port != 443 else ""
    return f"https://{p.hostname.lower()}{port}"


def validate_sha(sha: str | None) -> str | None:
    s = (sha or "").strip().lower()
    if not s:
        return None
    if not _SHA_RE.match(s):
        raise ValueError("expected_sha must be 7-40 hex characters")
    return s


def _sha_matches(observed: str, expected: str | None) -> bool:
    if not observed or not expected:
        return False
    o, e = observed.lower(), expected.lower()
    return o.startswith(e) or e.startswith(o)


def fetch_health(target: str, timeout: float = 10.0) -> dict:
    import requests
    try:
        r = requests.get(f"{target}/api/health", timeout=timeout, allow_redirects=False,
                         headers={"User-Agent": "stoic-deploy-watch/1"})
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        return {"http": r.status_code, "build_sha": (body.get("build_sha") or None),
                "app_env": body.get("app_env"), "status": body.get("status"), "error": None}
    except requests.RequestException as e:
        return {"http": 0, "build_sha": None, "app_env": None, "status": None,
                "error": type(e).__name__}


async def arm(db, *, target_url: str, expected_sha: str | None, notify_email: str,
              armed_by: str, timeout_h: int = 24) -> dict:
    target = validate_target(target_url)
    expected = validate_sha(expected_sha)
    if not (1 <= int(timeout_h) <= MAX_TIMEOUT_H):
        raise ValueError(f"timeout_h must be 1-{MAX_TIMEOUT_H}")
    now = _now()
    await db.deploy_watch.update_many(
        {"status": "watching"},
        {"$set": {"status": "cancelled", "ended_at": now, "ended_reason": "superseded"}})
    baseline = await asyncio.to_thread(fetch_health, target)
    doc = {"_id": uuid.uuid4().hex, "target_url": target, "expected_sha": expected,
           "notify_email": notify_email, "armed_by": armed_by, "armed_at": now,
           "expires_at": now + timedelta(hours=int(timeout_h)), "status": "watching",
           "baseline_sha": baseline.get("build_sha"), "baseline_http": baseline.get("http"),
           "last_poll_at": now, "last": baseline, "polls": 1, "ended_at": None,
           "ended_reason": None, "notified": None}
    if expected and _sha_matches(baseline.get("build_sha") or "", expected):
        doc.update(status="live", ended_at=now, ended_reason="already_live",
                   went_live_at=now, live_sha=baseline.get("build_sha"))
    await db.deploy_watch.insert_one(doc)
    return public(doc)


async def cancel(db, actor: str) -> dict | None:
    now = _now()
    doc = await db.deploy_watch.find_one_and_update(
        {"status": "watching"},
        {"$set": {"status": "cancelled", "ended_at": now, "ended_reason": f"cancelled by {actor}"}},
        return_document=True)
    return public(doc) if doc else None


async def latest(db) -> dict | None:
    doc = await db.deploy_watch.find_one({}, sort=[("armed_at", -1)])
    return public(doc) if doc else None


def public(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = doc["_id"]
    for k in ("armed_at", "expires_at", "last_poll_at", "ended_at", "went_live_at"):
        if isinstance(out.get(k), datetime):
            out[k] = out[k].isoformat()
    return out


def _email_html(kind: str, doc: dict, obs: dict) -> tuple[str, str, str]:
    tgt = html.escape(doc["target_url"])
    exp = html.escape(doc.get("expected_sha") or "any new build")
    sha = html.escape(str(obs.get("build_sha") or "—"))
    if kind == "live":
        subject = f"[STOIC] Deploy LIVE on {urlparse(doc['target_url']).hostname} · {sha[:12]}"
        head = "New build is live"
        body = (f"<p><b>{tgt}</b> now serves build <code>{sha}</code> "
                f"(app_env: {html.escape(str(obs.get('app_env') or '?'))}).</p>"
                f"<p>Expected: <code>{exp}</code>. Baseline before deploy: "
                f"<code>{html.escape(str(doc.get('baseline_sha') or '—'))}</code>.</p>"
                "<p>Next: open /admin/preflight on production and run "
                "<code>python scripts/live_probe.py --expect-sha &lt;sha&gt;</code>.</p>")
    else:
        subject = f"[STOIC] Deploy STALLED on {urlparse(doc['target_url']).hostname}"
        head = "Deploy watch timed out"
        body = (f"<p><b>{tgt}</b> did not serve build <code>{exp}</code> within the watch window.</p>"
                f"<p>Last observation: HTTP {html.escape(str(obs.get('http')))}, build "
                f"<code>{sha}</code>{' · ' + html.escape(obs['error']) if obs.get('error') else ''}.</p>"
                "<p>Check the deploy log and <code>python scripts/production_preflight.py</code>.</p>")
    html_doc = (f"<div style='font-family:monospace;background:#0A0A0A;color:#E4E4E7;padding:24px'>"
                f"<h2 style='color:#00FF41;margin:0 0 12px'>{head}</h2>{body}"
                f"<p style='color:#71717A;font-size:11px'>Deploy Watchdog · armed by "
                f"{html.escape(doc.get('armed_by') or '')} at {html.escape(str(doc.get('armed_at')))}</p></div>")
    text = re.sub(r"<[^>]+>", "", body)
    return subject, html_doc, text


async def _outbox_ack(db, outbox_id: str, fields: dict) -> None:
    await db.deploy_watch_outbox.update_one({"_id": outbox_id}, {"$set": fields})


async def _notify(db, doc: dict, kind: str, obs: dict) -> dict:
    """Exactly-once terminal notification (r16 P2-05): a unique outbox row keyed
    (watch_id, terminal_status) is claimed BEFORE the send; a send that succeeded
    just before a crash is never repeated, and the provider message id lives on
    the outbox row, separate from the watch state."""
    from pymongo.errors import DuplicateKeyError
    from email_sender import send_email
    outbox_id = f"{doc['_id']}:{kind}"
    now = _now()
    try:
        await db.deploy_watch_outbox.insert_one({"_id": outbox_id, "watch_id": doc["_id"], "kind": kind,
                                                 "state": "claimed", "claimed_at": now, "first_claimed_at": now,
                                                 "to": doc["notify_email"]})
    except DuplicateKeyError:
        row = await db.deploy_watch_outbox.find_one({"_id": outbox_id}) or {}
        if row.get("state") == "sent" or (row.get("state") == "claimed"
                                          and _aware(row.get("claimed_at") or now) > now - timedelta(minutes=5)):
            return {"kind": kind, "ok": row.get("state") == "sent", "deduped": True,
                    "provider_id": row.get("provider_id"), "at": row.get("sent_at") or row.get("claimed_at")}
        # r18 P2-02: automatic retries only INSIDE the provider's idempotency
        # retention window (Resend: 24 h). Older ambiguous rows become an INCIDENT
        # for manual resolution — never a blind re-send.
        first = _aware(row.get("first_claimed_at") or row.get("claimed_at") or now)
        if first < now - PROVIDER_IDEMPOTENCY_RETENTION:
            await db.deploy_watch_outbox.update_one(
                {"_id": outbox_id, "state": "claimed"},
                {"$set": {"state": "incident", "incident_at": now,
                          "incident_reason": "unacknowledged send older than provider idempotency retention"}})
            return {"kind": kind, "ok": False, "incident": True, "deduped": True}
        res = await db.deploy_watch_outbox.find_one_and_update(
            {"_id": outbox_id, "state": "claimed", "claimed_at": row.get("claimed_at")},
            {"$set": {"state": "claimed", "claimed_at": now, "retry": True,
                      "first_claimed_at": first}})
        if not res:
            return {"kind": kind, "ok": False, "deduped": True}
    subject, html_doc, text = _email_html(kind, doc, obs)
    res = await send_email(doc["notify_email"], subject, html_doc, text, idempotency_key=outbox_id)
    rec = {"kind": kind, "ok": bool(res.get("ok")), "error": res.get("error"),
           "provider_id": res.get("id"), "at": _now()}
    await _outbox_ack(db, outbox_id, {"state": "sent" if rec["ok"] else "failed", "provider_id": res.get("id"),
                                      "error": res.get("error"), "sent_at": rec["at"]})
    await db.deploy_watch.update_one({"_id": doc["_id"]}, {"$set": {"notified": rec}})
    return rec


async def poll_once(db, fetch=fetch_health) -> list[dict]:
    """Poll every watching row once; returns the rows that changed state."""
    changed = []
    async for doc in db.deploy_watch.find({"status": "watching"}):
        now = _now()
        obs = await asyncio.to_thread(fetch, doc["target_url"])
        upd = {"last_poll_at": now, "last": obs}
        sha = obs.get("build_sha") or ""
        expected = doc.get("expected_sha")
        went_live = (_sha_matches(sha, expected) if expected
                     else bool(sha) and sha != (doc.get("baseline_sha") or "") and obs.get("http") == 200)
        if went_live:
            upd.update(status="live", went_live_at=now, live_sha=sha, ended_at=now, ended_reason="build_live")
        elif now >= _aware(doc["expires_at"]):
            upd.update(status="stalled", ended_at=now, ended_reason="timeout")
        res = await db.deploy_watch.find_one_and_update(
            {"_id": doc["_id"], "status": "watching"},
            {"$set": upd, "$inc": {"polls": 1}}, return_document=True)
        if res and res["status"] in ("live", "stalled"):
            await _notify(db, res, res["status"], obs)
            changed.append(public(res))
    return changed


async def loop():
    interval = int(os.environ.get("DEPLOY_WATCH_INTERVAL_SEC", "30"))
    from database import get_db
    while True:
        try:
            await asyncio.sleep(interval)
            await poll_once(get_db())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("deploy watch loop error: %s", e)
