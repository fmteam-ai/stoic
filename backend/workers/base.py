"""Shared worker runtime: leader lease + graceful loop supervision."""
import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from database import get_db  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker")

LEASE_TTL_SEC = 45
LEASE_RENEW_SEC = 15
HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def _try_acquire(db, name: str) -> bool:
    now = datetime.now(timezone.utc)
    res = await db.worker_leases.update_one(
        {"_id": name,
         "$or": [{"holder": HOLDER},
                 {"expires_at": {"$lt": now.isoformat()}},
                 {"expires_at": {"$exists": False}}]},
        {"$set": {"holder": HOLDER,
                  "expires_at": (now + timedelta(seconds=LEASE_TTL_SEC)).isoformat(),
                  "renewed_at": now.isoformat()}},
        upsert=False)
    if res.matched_count == 1:
        return True
    try:
        await db.worker_leases.insert_one(
            {"_id": name, "holder": HOLDER,
             "expires_at": (now + timedelta(seconds=LEASE_TTL_SEC)).isoformat(),
             "renewed_at": now.isoformat()})
        return True
    except Exception:
        return False


async def _lease_keeper(name: str, lost: asyncio.Event):
    db = get_db()
    while True:
        await asyncio.sleep(LEASE_RENEW_SEC)
        try:
            ok = await _try_acquire(db, name)
            if not ok:
                logger.error("worker %s lost its lease — stopping loops", name)
                lost.set()
                return
        except Exception as e:
            logger.warning("lease renew error for %s: %s", name, e)


async def run_worker(name: str, loop_factories: list) -> None:
    """Acquire the leader lease for `name`, then run all loops until the
    lease is lost or the process is terminated. loop_factories: list of
    zero-arg callables returning coroutines."""
    db = get_db()
    logger.info("worker %s starting (holder=%s)", name, HOLDER)
    while not await _try_acquire(db, name):
        logger.info("worker %s standing by — another holder owns the lease",
                    name)
        await asyncio.sleep(LEASE_RENEW_SEC)
    logger.info("worker %s acquired leader lease", name)
    lost = asyncio.Event()
    tasks = [asyncio.create_task(f()) for f in loop_factories]
    tasks.append(asyncio.create_task(_lease_keeper(name, lost)))
    lost_waiter = asyncio.create_task(lost.wait())
    try:
        done, _ = await asyncio.wait([*tasks, lost_waiter],
                                     return_when=asyncio.FIRST_COMPLETED)
        for d in done:
            if d is not lost_waiter and d.exception():
                logger.error("worker %s loop crashed: %s", name, d.exception())
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("worker %s stopped", name)


def main(name: str, loop_factories: list) -> None:
    try:
        asyncio.run(run_worker(name, loop_factories))
    except KeyboardInterrupt:
        pass
