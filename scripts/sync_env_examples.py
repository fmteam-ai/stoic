#!/usr/bin/env python3
"""Single source of truth for the env templates: deploy/env/*.env.example.

The platform's auto-commit skips every `.env*` path, so edits made directly to
`.env.example` / `backend/.env.example` never reach the repository and CI (which
reads the templates) goes red. Edit the files under deploy/env/ instead and run
this script; CI and deploy/install.sh run it before anything reads a template.

  python scripts/sync_env_examples.py          # write .env.example + backend/.env.example
  python scripts/sync_env_examples.py --check  # exit 1 if a dot-file drifted from its source
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = {
    os.path.join("deploy", "env", "root.env.example"): ".env.example",
    os.path.join("deploy", "env", "backend.env.example"): os.path.join("backend", ".env.example"),
}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main(argv):
    check = "--check" in argv
    drift = []
    for src_rel, dst_rel in TEMPLATES.items():
        src, dst = os.path.join(ROOT, src_rel), os.path.join(ROOT, dst_rel)
        want = _read(src)
        have = _read(dst) if os.path.exists(dst) else None
        if have == want:
            continue
        if check:
            drift.append(f"{dst_rel} differs from {src_rel}")
            continue
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(want)
        print(f"wrote {dst_rel} from {src_rel}")
    if drift:
        print("ENV TEMPLATE DRIFT:\n  " + "\n  ".join(drift) + "\nfix: edit deploy/env/*.env.example then run scripts/sync_env_examples.py")
        return 1
    if check:
        print("OK: .env.example files match deploy/env/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
