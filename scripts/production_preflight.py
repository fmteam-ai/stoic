#!/usr/bin/env python3
"""Print exactly which APP_ENV=production boot guardrails the values in
backend/.env would violate — the same validator the API runs at startup.

    python scripts/production_preflight.py            # human table
    python scripts/production_preflight.py --json     # machine readable
Exit 1 when the verdict is will_crash. Secrets are masked; nothing is written.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, "backend", ".env"), override=False)
    os.environ["APP_ENV"] = "production"
    from deploy_preflight import run_preflight
    report = run_preflight()
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
    else:
        for c in report["checks"]:
            if c["status"] == "pass":
                continue
            print(f"[{c['status'].upper():5}] {c['label']}")
            print(f"        current : {c['current']}")
            print(f"        required: {c['required']}")
            print(f"        fix     : {c['fix']}")
        print(f"\nverdict={report['verdict']} fail={report['fail_count']} warn={report['warn_count']}")
        print(report["note"])
    return 1 if report["verdict"] == "will_crash" else 0


if __name__ == "__main__":
    sys.exit(main())
