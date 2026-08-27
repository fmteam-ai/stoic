"""Distributed token-bucket rate limiter for the Enterprise API.

Backend order:
  1. Redis (REDIS_URL set) — atomic Lua token bucket, multi-process safe.
  2. MongoDB fallback — aggregation-pipeline refill + guarded atomic
     consume; correct across processes, slightly higher latency.

Bucket identity: API key + tenant + endpoint class, so one hot tenant or
one chatty write path can never starve everything else on the key."""
import logging
import os
import time

logger = logging.getLogger("rate.limiter.distributed")

_redis = None
_redis_failed_at = 0.0
_REDIS_RETRY_S = 30

# Atomic token bucket: refill by elapsed time, consume 1 if available.
_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local b = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(b[1])
local ts = tonumber(b[2])
if tokens == nil then tokens = capacity ts = now_ms end
tokens = math.min(capacity, tokens + (now_ms - ts) * refill_per_ms)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1 allowed = 1 end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', key, 120000)
return {allowed, tostring(tokens)}
"""


async def _get_redis():
    global _redis, _redis_failed_at
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    if _redis is not None:
        return _redis
    if time.time() - _redis_failed_at < _REDIS_RETRY_S:
        return None
    try:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(url, socket_timeout=1,
                                   socket_connect_timeout=1)
        await _redis.ping()
        logger.info("enterprise rate limiter using Redis backend")
        return _redis
    except Exception as e:
        logger.warning("Redis unavailable (%s) — Mongo fallback active", e)
        _redis = None
        _redis_failed_at = time.time()
        return None


async def _allow_redis(r, bucket: str, capacity: int):
    try:
        now_ms = int(time.time() * 1000)
        refill = capacity / 60_000.0   # capacity per minute
        allowed, tokens = await r.eval(_LUA, 1, f"rl:{bucket}",
                                       capacity, refill, now_ms)
        return bool(int(allowed)), float(tokens)
    except Exception as e:
        global _redis, _redis_failed_at
        logger.warning("Redis rate-limit call failed (%s) — falling back "
                       "to Mongo", e)
        _redis = None
        _redis_failed_at = time.time()
        return None


async def _allow_mongo(db, bucket: str, capacity: int):
    now_ms = int(time.time() * 1000)
    refill = capacity / 60_000.0
    # 1 — refill via aggregation-pipeline update (atomic per document)
    await db.rate_buckets.update_one(
        {"_id": bucket},
        [{"$set": {
            "tokens": {"$min": [capacity, {"$add": [
                {"$ifNull": ["$tokens", capacity]},
                {"$multiply": [refill, {"$max": [0, {"$subtract": [
                    now_ms, {"$ifNull": ["$ts", now_ms]}]}]}]}]}]},
            "ts": now_ms}}],
        upsert=True)
    # 2 — guarded atomic consume: only succeeds while tokens ≥ 1
    res = await db.rate_buckets.find_one_and_update(
        {"_id": bucket, "tokens": {"$gte": 1}},
        {"$inc": {"tokens": -1}})
    if res is None:
        doc = await db.rate_buckets.find_one({"_id": bucket},
                                             {"tokens": 1})
        return False, float((doc or {}).get("tokens") or 0)
    return True, float(res.get("tokens") or 0) - 1


async def allow_request(db, *, key_id: str, tenant: str,
                        endpoint_class: str,
                        limit_per_minute: int) -> tuple:
    """Returns (allowed: bool, meta: dict). Never raises."""
    capacity = max(1, int(limit_per_minute))
    bucket = f"{key_id}:{tenant}:{endpoint_class}"
    backend = "mongo"
    out = None
    r = await _get_redis()
    if r is not None:
        out = await _allow_redis(r, bucket, capacity)
        if out is not None:
            backend = "redis"
    if out is None:
        try:
            out = await _allow_mongo(db, bucket, capacity)
        except Exception as e:
            # limiter infrastructure failure must not take the API down —
            # fail open but log loudly (visible in ops telemetry)
            logger.error("rate limiter unavailable (%s) — failing open "
                         "for one request", e)
            return True, {"backend": "none", "degraded": True}
    allowed, tokens = out
    return allowed, {"backend": backend, "remaining": round(tokens, 1),
                     "limit_per_minute": capacity,
                     "endpoint_class": endpoint_class}
