import os

_client = None
_db = None


def get_client():
    global _client
    if _client is None:
        # lazy import (H7): pure-logic unit tests must not require motor
        from motor.motor_asyncio import AsyncIOMotorClient
        listeners = []
        try:  # iter-171 (#9): slow-query monitoring
            from query_perf import SlowQueryListener
            listeners = [SlowQueryListener()]
        except Exception:  # noqa: BLE001
            pass
        _client = AsyncIOMotorClient(os.environ["MONGO_URL"],
                                     event_listeners=listeners)
    return _client


def get_db():
    global _db
    if _db is None:
        _db = get_client()[os.environ["DB_NAME"]]
    return _db


async def close_client():
    global _client
    if _client is not None:
        _client.close()
        _client = None
