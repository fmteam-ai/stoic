"""SA3 — daily (07:00 UTC, email + Telegram) and weekly (Monday, email) reports; stored in
security_reports for download from the panel. Builders are pure over db rows; delivery reuses alerts."""
import html
import logging
from datetime import datetime, timedelta, timezone

from security_agent import alerts
from security_agent.checks import CHECKS
from security_agent.config import AREAS, SEVERITIES
from security_agent.redact import mask

log = logging.getLogger("security_agent.reports")
STATE_ID = "security_agent_reports"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _mins(a: str, b: str) -> float | None:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


async def build_daily(db, cfg: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = _iso(now - timedelta(days=1))
    rows = await db.security_findings.find({"last_seen": {"$gte": since}}).limit(2000).to_list(length=2000)
    new = [r for r in rows if r.get("first_seen", "") >= since]
    resolved = [r for r in rows if r.get("status") in ("resolved", "false_positive") and (r.get("resolved_at") or "") >= since]
    still_open = await db.security_findings.find({"status": {"$in": ["open", "contained", "acknowledged"]}}).limit(500).to_list(length=500)
    still_open.sort(key=lambda r: -SEVERITIES.index(r.get("severity", "low")))
    actions = await db.security_actions.find({"kind": "containment", "at": {"$gte": since}}).limit(500).to_list(length=500)
    runs = await db.security_check_runs.find({"_id": {"$regex": "^latest:"}}).limit(100).to_list(length=100)
    failed = [r["check_id"] for r in runs if r.get("status") == "failed"]
    silent = [cid for cid in CHECKS if cid not in {r["check_id"] for r in runs}]
    errors = [r for r in still_open if r.get("check_id") == "P5"]
    return {"kind": "daily", "period_start": since, "period_end": _iso(now), "built_at": _iso(now),
            "new_findings": [_brief(r) for r in new[:50]], "auto_contained": [_brief_action(a) for a in actions if a.get("status") not in ("refused_cap", "refused_protected")][:50],
            "refused": [_brief_action(a) for a in actions if a.get("status") in ("refused_cap", "refused_protected")][:20],
            "resolved": [_brief(r) for r in resolved[:50]], "still_open": [_brief(r, solution=True) for r in still_open[:50]],
            "top_error_groups": [_brief(r) for r in errors[:10]], "checks_failed": failed, "checks_silent": silent,
            "mode": cfg.get("mode"), "counts": {"new": len(new), "resolved": len(resolved), "open": len(still_open), "actions": len(actions)}}


async def build_weekly(db, cfg: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since, prev = _iso(now - timedelta(days=7)), _iso(now - timedelta(days=14))
    rows = await db.security_findings.find({"first_seen": {"$gte": prev}}).limit(5000).to_list(length=5000)
    this_w = [r for r in rows if r.get("first_seen", "") >= since]
    last_w = [r for r in rows if r.get("first_seen", "") < since]
    trends = {a: {"this_week": sum(r.get("area") == a for r in this_w), "last_week": sum(r.get("area") == a for r in last_w)} for a in AREAS}
    ttr = [m for r in this_w if r.get("resolved_at") and (m := _mins(r["first_seen"], r["resolved_at"])) is not None]
    by_check: dict = {}
    for r in this_w:
        d = by_check.setdefault(r.get("check_id"), {"total": 0, "false_positive": 0})
        d["total"] += 1
        d["false_positive"] += int(r.get("status") == "false_positive")
    fp_rate = {c: round(d["false_positive"] / d["total"], 2) for c, d in by_check.items() if d["total"]}
    cves = await db.security_findings.find({"check_id": {"$in": ["D1", "D2"]}, "status": {"$in": ["open", "contained", "acknowledged"]}}).limit(100).to_list(length=100)
    tuning = [f"{c}: {int(r * 100)}% false positives this week — raise its threshold or review the rule" for c, r in fp_rate.items() if r >= 0.5]
    tuning += [f"{c}: {by_check[c]['total']} findings this week with no false positives — threshold may be too loose only if the alerts were noise"
               for c in by_check if by_check[c]["total"] >= 20 and fp_rate.get(c, 0) == 0]
    return {"kind": "weekly", "period_start": since, "period_end": _iso(now), "built_at": _iso(now), "trends_by_area": trends,
            "mean_time_to_resolve_min": round(sum(ttr) / len(ttr), 1) if ttr else None, "false_positive_rate": fp_rate,
            "open_cves": [_brief(r) for r in cves], "tuning_suggestions": tuning, "mode": cfg.get("mode"),
            "counts": {"this_week": len(this_w), "last_week": len(last_w), "resolved": len(ttr)}}


def _brief(r: dict, solution: bool = False) -> dict:
    out = {"id": str(r.get("_id")), "check_id": r.get("check_id"), "severity": r.get("severity"), "area": r.get("area"), "title": r.get("title"),
           "what_happened": r.get("what_happened"), "status": r.get("status"), "occurrences": r.get("occurrences"), "first_seen": r.get("first_seen")}
    if solution:
        out["solution"] = r.get("solution") or []
    return out


def _brief_action(a: dict) -> dict:
    return {"rule": a.get("rule"), "action": a.get("action"), "target": a.get("target"), "status": a.get("status"), "at": a.get("at"), "check_id": a.get("check_id")}


def render_text(rep: dict) -> str:
    c = rep.get("counts") or {}
    if rep["kind"] == "daily":
        lines = [f"[STOIC SECURITY] Daily report · {rep['period_end'][:10]} · mode {rep.get('mode')}",
                 f"New {c.get('new', 0)} · Resolved {c.get('resolved', 0)} · Open {c.get('open', 0)} · Actions {c.get('actions', 0)}"]
        for label, key in (("NEW", "new_findings"), ("WOULD HAVE DONE / CONTAINED", "auto_contained"), ("REFUSED", "refused"), ("RESOLVED", "resolved")):
            items = rep.get(key) or []
            if items:
                lines.append(f"-- {label} ({len(items)})")
                lines += [f"  {i.get('severity', i.get('status', '')).upper()} {i.get('check_id')} {i.get('title') or i.get('action')} {i.get('target', '')}".rstrip() for i in items[:15]]
        if rep.get("still_open"):
            lines.append(f"-- STILL OPEN ({len(rep['still_open'])})")
            for i in rep["still_open"][:15]:
                lines.append(f"  {i['severity'].upper()} {i['check_id']} {i['title']} — {i['what_happened']}")
                lines += [f"     {n}) {s}" for n, s in enumerate(i.get("solution") or [], 1)]
        if rep.get("checks_failed") or rep.get("checks_silent"):
            lines.append(f"-- CHECKS failed: {', '.join(rep['checks_failed']) or 'none'} · never ran: {', '.join(rep['checks_silent']) or 'none'}")
    else:
        lines = [f"[STOIC SECURITY] Weekly report · {rep['period_end'][:10]}",
                 f"Findings this week {c.get('this_week', 0)} (last week {c.get('last_week', 0)}) · mean time to resolve "
                 f"{rep.get('mean_time_to_resolve_min') if rep.get('mean_time_to_resolve_min') is not None else '—'} min",
                 "-- TRENDS BY AREA"] + [f"  {a}: {t['this_week']} (prev {t['last_week']})" for a, t in rep["trends_by_area"].items()]
        if rep.get("false_positive_rate"):
            lines.append("-- FALSE-POSITIVE RATE PER RULE")
            lines += [f"  {k}: {int(v * 100)}%" for k, v in rep["false_positive_rate"].items()]
        lines.append(f"-- OPEN DEPENDENCY CVEs: {len(rep.get('open_cves') or [])}")
        lines += [f"  {i['title']} — {i['what_happened']}" for i in rep.get("open_cves") or []][:10]
        if rep.get("tuning_suggestions"):
            lines.append("-- TUNING")
            lines += [f"  {t}" for t in rep["tuning_suggestions"]]
    return mask("\n".join(lines))


def render_html(rep: dict) -> str:
    return ("<!doctype html><html><head><meta charset='utf-8'><title>STOIC Security report</title></head>"
            "<body style='background:#050505;color:#e4e4e7;font-family:monospace;padding:24px'>"
            f"<pre style='white-space:pre-wrap'>{html.escape(render_text(rep))}</pre></body></html>")


async def store(db, rep: dict) -> str:
    doc = {**rep, "html": render_html(rep), "text": render_text(rep), "expires_at": datetime.now(timezone.utc) + timedelta(days=180)}
    res = await db.security_reports.insert_one(doc)
    return str(res.inserted_id)


async def deliver(db, cfg: dict, rep: dict, *, telegram: bool, send_tg=None, send_mail=None) -> dict:
    send_tg, send_mail = send_tg or alerts.send_telegram, send_mail or alerts.send_email
    text = render_text(rep)
    tg = await send_tg(text) if telegram else False
    mail = await send_mail(cfg, f"[STOIC SECURITY] {rep['kind'].title()} report {rep['period_end'][:10]}", text)
    rid = await store(db, {**rep, "delivered": {"telegram": tg, "email": mail}})
    return {"id": rid, "telegram": tg, "email": mail}


def _due_daily(cfg: dict, now: datetime, last_date: str | None) -> bool:
    hh, _, mm = str(cfg.get("daily_report_utc") or "07:00").partition(":")
    slot = now.replace(hour=int(hh or 7), minute=int(mm or 0), second=0, microsecond=0)
    return now >= slot and last_date != now.date().isoformat()


def _due_weekly(now: datetime, last_week: str | None) -> bool:
    return now.weekday() == 0 and now.hour >= 7 and last_week != f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"


async def scheduler_tick(db, cfg: dict, now: datetime | None = None, **sends) -> list[str]:
    """Idempotent across workers: the date/week claim is written before delivery."""
    now = now or datetime.now(timezone.utc)
    state = await db.platform_state.find_one({"_id": STATE_ID}) or {}
    sent = []
    if _due_daily(cfg, now, state.get("last_daily_date")):
        await db.platform_state.update_one({"_id": STATE_ID}, {"$set": {"last_daily_date": now.date().isoformat()}}, upsert=True)
        await deliver(db, cfg, await build_daily(db, cfg, now), telegram=True, **sends)
        sent.append("daily")
    wk = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    if _due_weekly(now, state.get("last_weekly_week")):
        await db.platform_state.update_one({"_id": STATE_ID}, {"$set": {"last_weekly_week": wk}}, upsert=True)
        await deliver(db, cfg, await build_weekly(db, cfg, now), telegram=False, **sends)
        sent.append("weekly")
    return sent
