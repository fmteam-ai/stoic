"""Infrastructure page — shadow certification clock check reads the broker clock the EA reports (same source as the
account certification badge, v1.60.26), and the Backups panel distinguishes the server's own Mongo backups
(STOIC_BACKUP_DIR, read-only) from VPS-fleet per-deployment backups."""
import asyncio
import gzip
import os
import sys
import time
from datetime import datetime, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cert(db, monkeypatch):
    import routes.infra_routes as ir
    monkeypatch.setattr(ir, "get_db", lambda: db)
    return _run(ir.shadow_certification(user={"id": "u1", "role": "user"}))


def test_clock_check_passes_from_ea_heartbeat_broker_time_without_any_vps_agent(monkeypatch):
    db = FakeDb()
    db.accounts.rows.append({"_id": ObjectId(), "user_id": "u1", "label": "RoboForex demo", "mode": "live", "status": "active",
                             "broker_time_info": {"server_gmt_offset_sec": 10799}})      # EA 1.64 heartbeat, no vps_agents row
    out = _cert(db, monkeypatch)
    assert out["checks"]["clock_synchronization"] is True
    assert out["details"]["clock_synchronization"] == {"source": "ea_broker_time", "offset_sec": 10800, "account": "RoboForex demo"}


def test_clock_check_falls_back_to_learned_offset_then_agent_then_fails_honestly(monkeypatch):
    db = FakeDb()
    db.accounts.rows.append({"_id": ObjectId(), "user_id": "u1", "label": "A", "mode": "live", "status": "active",
                             "broker_utc_offset_sec": 7200})
    out = _cert(db, monkeypatch)
    assert out["checks"]["clock_synchronization"] is True and out["details"]["clock_synchronization"]["source"] == "learned_from_deals"
    db = FakeDb()
    db.accounts.rows.append({"_id": ObjectId(), "user_id": "u1", "label": "A", "mode": "live", "status": "active"})
    db.vps_agents.rows.append({"user_id": "u1", "registered_at": "2026-01-01", "last_metrics": {"clock_offset_ms": 250}})
    out = _cert(db, monkeypatch)
    assert out["checks"]["clock_synchronization"] is True and out["details"]["clock_synchronization"]["source"] == "vps_agent"
    db = FakeDb()
    db.accounts.rows.append({"_id": ObjectId(), "user_id": "u1", "label": "paper", "mode": "paper", "status": "active",
                             "broker_time_info": {"server_gmt_offset_sec": 0}})          # paper accounts have no broker clock
    out = _cert(db, monkeypatch)
    assert out["checks"]["clock_synchronization"] is False and out["details"]["clock_synchronization"]["source"] == "unknown"


def test_server_backups_lists_archives_newest_first_and_reports_missing_mount(tmp_path, monkeypatch):
    import routes.infra_routes as ir
    monkeypatch.setenv("STOIC_BACKUP_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    for name, age_h in (("mongo-2026-10-12.archive.gz", 30), ("mongo-2026-10-13.archive.gz", 2)):
        p = tmp_path / name
        with gzip.open(p, "wb") as fh:
            fh.write(b"x" * 1024)
        os.utime(p, (time.time() - age_h * 3600, time.time() - age_h * 3600))
    (tmp_path / "notes.txt").write_text("ignored")
    out = ir.server_backups(now)
    assert out["mounted"] is True and out["count"] == 2 and [f["name"] for f in out["files"]][0] == "mongo-2026-10-13.archive.gz"
    assert out["newest_age_h"] == 2.0 and all(f["size_bytes"] > 0 for f in out["files"])
    monkeypatch.setenv("STOIC_BACKUP_DIR", str(tmp_path / "missing"))
    out = ir.server_backups(now)
    assert out == {"dir": str(tmp_path / "missing"), "mounted": False, "files": [], "newest_age_h": None}


def test_overview_exposes_server_backups_to_admins_only(monkeypatch, tmp_path):
    import routes.infra_routes as ir
    db = FakeDb()
    monkeypatch.setattr(ir, "get_db", lambda: db)
    monkeypatch.setenv("STOIC_BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(ir, "_admin_ok", lambda u: u.get("role") == "admin")
    assert _run(ir.infra_overview(user={"id": "u1", "role": "user"}))["server_backups"] is None
    adm = _run(ir.infra_overview(user={"id": "u1", "role": "admin"}))["server_backups"]
    assert adm["mounted"] is True and adm["files"] == []


def test_compose_mounts_backups_read_only_into_the_api_and_frontend_labels_both_sections():
    compose = open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8").read()
    api = compose.split("\n  backend:")[1].split("\n  worker-trading:")[0]
    assert "STOIC_BACKUP_DIR: /backups" in api and "${BACKUP_DIR:-./backups}:/backups:ro" in api
    assert "./deploy/state:/app/state:ro" in api                               # the app-common mount is kept
    src = open(os.path.join(ROOT, "frontend", "src", "pages", "Infrastructure.jsx"), encoding="utf-8").read()
    assert "SERVER · MONGO BACKUPS" in src and "VPS FLEET · PER-DEPLOYMENT BACKUPS" in src
    assert "No VPS fleet backups yet." in src and "No backups yet." not in src
    assert 'data-testid="server-backups-unmounted"' in src and 'data-testid="cert-clock-detail"' in src
    assert "cert.details?.clock_synchronization" in src
