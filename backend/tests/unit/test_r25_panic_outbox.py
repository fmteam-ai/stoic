"""r25 P2-02 — PANIC outbox: transient delivery errors retry with backoff,
terminal `failed` only after the attempt budget; health counts exposed."""
import asyncio
import pytest
import routes.panic_routes as pr


class _Coll:
    def __init__(self, row): self.row = row; self.updates = []
    async def find_one_and_update(self, q, u):
        if self.row["state"] not in ("pending", "publishing"): return None
        self.row.update(u["$set"]); self.row["attempts"] = self.row.get("attempts", 0) + u["$inc"]["attempts"]
        return dict(self.row, attempts=self.row["attempts"] - 1)   # pre-increment view like Mongo default
    async def update_one(self, q, u):
        self.updates.append(u); self.row.update(u.get("$set", {}))
        for k in u.get("$unset", {}): self.row.pop(k, None)
    async def count_documents(self, q): return 1 if self.row["state"] == q["state"] else 0


class _DB:
    def __init__(self, row): self.ops_outbox = _Coll(row)


def _row(attempts=0, state="pending"):
    return {"_id": "ob1", "kind": "panic_lock", "state": state, "attempts": attempts, "user_id": "u1", "payload": {"x": 1}}


def test_transient_error_goes_back_to_pending_with_backoff(monkeypatch):
    async def boom(*a, **k): raise ConnectionError("ws down")
    monkeypatch.setattr(pr.ws_manager, "broadcast", boom)
    db = _DB(_row(attempts=1))
    assert asyncio.run(pr.publish_panic_outbox(db, "ob1")) is False
    r = db.ops_outbox.row
    assert r["state"] == "pending" and "not_before" in r and r["last_error"].startswith("ConnectionError")
    assert "lease_until" not in r      # lease released so the sweeper may re-claim after not_before
    assert r["attempts"] == 2


def test_success_is_published_delivered(monkeypatch):
    async def ok(*a, **k): return None
    monkeypatch.setattr(pr.ws_manager, "broadcast", ok)
    db = _DB(_row())
    assert asyncio.run(pr.publish_panic_outbox(db, "ob1")) is True
    assert db.ops_outbox.row["state"] == "published" and db.ops_outbox.row["outcome"] == "delivered"


def test_budget_exhausted_is_terminal_failed(monkeypatch):
    called = []
    async def ok(*a, **k): called.append(1)
    monkeypatch.setattr(pr.ws_manager, "broadcast", ok)
    db = _DB(_row(attempts=pr.OUTBOX_MAX_ATTEMPTS))
    assert asyncio.run(pr.publish_panic_outbox(db, "ob1")) is False
    assert db.ops_outbox.row["state"] == "failed" and not called


def test_health_counts():
    db = _DB(_row(state="failed"))
    h = asyncio.run(pr.ops_outbox_health(db))
    assert h["failed"] == 1 and h["ok"] is False


def test_sweeper_respects_not_before():
    src = open("routes/panic_routes.py").read()
    assert '"not_before": {"$lte": now}' in src and '"not_before": {"$exists": False}' in src
    assert 'checks["panic_outbox"]' in open("routes/ops_routes.py").read()
