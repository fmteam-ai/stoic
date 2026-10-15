#!/usr/bin/env python3
"""v1.60.41 — safe re-anchor of the admin audit chain after a link break (15 Oct duplicate seq 77/78).

    python scripts/reanchor_audit_chain.py                      # verify + dry-run plan (nothing written)
    python scripts/reanchor_audit_chain.py --apply --actor ops@… --reason "15 Oct seq race (dup 77/78)"

Dry-run prints the break and how many entries would be re-linked. --apply snapshots the originals into
audit_chain_repairs, rewrites seq/prev_hash/entry_hash from the break onward (each rewritten entry keeps
its original seq/hashes under `reanchor`), appends a chained `audit_chain_reanchored` marker, builds the
unique seq index and anchors the new head. Refuses when any entry's CONTENT no longer matches its own
hash (that is tampering, not a race — restore from backup). Reads MONGO_URL / DB_NAME from backend/.env.
"""
import argparse
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))


def _load_env():
    p = os.path.join(ROOT, "backend", ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def _main(args) -> int:
    from database import get_db
    from audit_chain import verify_chain, repair_chain
    db = get_db()
    before = await verify_chain(db)
    print("verify:", json.dumps({k: before[k] for k in ("ok", "chained_entries", "first_bad_seq")}), "anomalies:", before["anomalies"][:6])
    res = await repair_chain(db, actor=args.actor, reason=args.reason, apply=args.apply)
    print(("APPLIED" if res["applied"] else "DRY-RUN") + ":", json.dumps(res, default=str, indent=2))
    if not res["ok"]:
        print("REFUSED —", res["reason"])
        return 2
    if args.apply:
        after = await verify_chain(db)
        from audit_anchor import verify_latest
        anchor = await verify_latest(db)
        print("verify after:", json.dumps({k: after[k] for k in ("ok", "chained_entries", "first_bad_seq")}))
        print("anchor:", json.dumps(anchor, default=str))
        return 0 if after["ok"] and anchor.get("ok") else 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--actor", default=os.environ.get("USER", "operator"))
    ap.add_argument("--reason", default="audit chain link break re-anchor")
    args = ap.parse_args()
    _load_env()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
