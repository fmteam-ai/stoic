#!/usr/bin/env python3
"""iter-160 — performance benchmark harness (stdlib + httpx only).

Simulates realistic mixed user traffic against the API and reports
p50/p95/p99 latency, throughput and error rate per endpoint, then compares
against the recorded baseline (docs/BENCHMARK_BASELINE.json).

Usage:
  python scripts/benchmark.py --base http://localhost:8001 --users 20 --seconds 30
  python scripts/benchmark.py --record-baseline        # overwrite baseline
"""
import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time

import httpx

BASELINE_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "docs", "BENCHMARK_BASELINE.json")

# realistic mix: weight, method, path, auth required
MIX = [
    (30, "GET", "/api/health", False),
    (15, "GET", "/api/plans", False),
    (10, "GET", "/api/auth/turnstile-config", False),
    (15, "GET", "/api/status/summary", False),
    (10, "GET", "/api/kb/articles", False),
    (10, "GET", "/api/bot/pulse", True),
    (5, "GET", "/api/accounts", True),
    (5, "GET", "/api/trades?limit=20", True),
]


async def login(client, base, email, password):
    r = await client.post(f"{base}/api/auth/login",
                          json={"email": email, "password": password})
    return r.status_code == 200


async def user_loop(base, auth_creds, until, results):
    async with httpx.AsyncClient(timeout=15) as client:
        authed = False
        if auth_creds:
            authed = await login(client, base, *auth_creds)
        weights = [w for w, *_ in MIX]
        while time.time() < until:
            _, method, path, needs_auth = random.choices(MIX, weights)[0]
            if needs_auth and not authed:
                continue
            t0 = time.perf_counter()
            try:
                r = await client.request(method, f"{base}{path}")
                ms = (time.perf_counter() - t0) * 1000
                results.append((path, ms, r.status_code))
            except httpx.HTTPError:
                results.append((path, (time.perf_counter() - t0) * 1000, 599))
            await asyncio.sleep(random.uniform(0.05, 0.4))


def summarize(results):
    by_path = {}
    for path, ms, status in results:
        by_path.setdefault(path, []).append((ms, status))
    out = {}
    for path, rows in sorted(by_path.items()):
        durs = sorted(ms for ms, _ in rows)
        errs = sum(1 for _, s in rows if s >= 500)
        n = len(durs)
        out[path] = {
            "count": n,
            "p50_ms": round(durs[n // 2], 1),
            "p95_ms": round(durs[min(n - 1, int(n * 0.95))], 1),
            "p99_ms": round(durs[min(n - 1, int(n * 0.99))], 1),
            "mean_ms": round(statistics.fmean(durs), 1),
            "error_rate_pct": round(100 * errs / n, 2),
        }
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8001")
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--email", default="admin@trading.bot")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--record-baseline", action="store_true")
    args = ap.parse_args()

    results = []
    until = time.time() + args.seconds
    # only 2 authed users to avoid login rate limits; rest anonymous
    tasks = [user_loop(args.base, (args.email, args.password) if i < 2 else
                       None, until, results) for i in range(args.users)]
    t0 = time.time()
    await asyncio.gather(*tasks)
    wall = time.time() - t0
    summary = summarize(results)
    report = {"users": args.users, "seconds": args.seconds,
              "total_requests": len(results),
              "rps": round(len(results) / wall, 1),
              "endpoints": summary,
              "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime())}
    print(json.dumps(report, indent=2))

    if args.record_baseline:
        with open(BASELINE_PATH, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nbaseline recorded → {BASELINE_PATH}")
        return 0

    if os.path.exists(BASELINE_PATH):
        with open(BASELINE_PATH) as f:
            base = json.load(f)
        print("\n── vs baseline ──")
        regressions = 0
        for path, cur in summary.items():
            ref = base.get("endpoints", {}).get(path)
            if not ref or cur["count"] < 10:
                continue
            worse = cur["p95_ms"] > ref["p95_ms"] * 1.5 + 50
            flag = "REGRESSION" if worse else "ok"
            regressions += worse
            print(f"{flag:>10}  {path}  p95 {ref['p95_ms']}ms → {cur['p95_ms']}ms")
        if regressions:
            print(f"\n{regressions} endpoint(s) regressed >50% vs baseline")
            return 1
        print("\nno latency regressions vs baseline")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
