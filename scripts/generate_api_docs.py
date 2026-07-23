"""Generate docs/API.md from the FastAPI OpenAPI schema. Run:
    cd backend && python ../scripts/generate_api_docs.py
"""
import os
import sys
from collections import defaultdict

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

from server import app  # noqa: E402

schema = app.openapi()
paths = schema.get("paths", {})

groups = defaultdict(list)
for path, methods in sorted(paths.items()):
    for method, op in methods.items():
        if method not in ("get", "post", "put", "patch", "delete"):
            continue
        tag = (op.get("tags") or ["misc"])[0]
        summary = op.get("summary") or ""
        desc = (op.get("description") or "").strip().splitlines()
        first = desc[0].strip() if desc else ""
        groups[tag].append((method.upper(), path, summary, first))

out = ["# STOIC — API Reference",
       "",
       f"Auto-generated from the FastAPI OpenAPI schema "
       f"({len([1 for g in groups.values() for _ in g])} operations). "
       "Regenerate with `python scripts/generate_api_docs.py`.",
       "",
       "## Conventions",
       "- All routes are prefixed with `/api` and served on port 8001.",
       "- **Auth**: httpOnly `access_token` cookie (JWT) + CSRF double-submit "
       "header `X-CSRF-Token` on mutating requests. Enterprise `/v1/*` routes "
       "use `X-API-Key` instead.",
       "- **Step-up MFA**: live activation, risk raises, panic release and "
       "API-key creation additionally require `X-Step-Up-Token` obtained from "
       "`POST /api/auth/step-up` (fresh TOTP, single-use, 5 min).",
       "- **Bridge routes** (`/api/bridge/*`) authenticate the MT5 EA with a "
       "per-account `bridge_token`.",
       "- `GET /api/metrics` (Prometheus) is gated by `X-Metrics-Token`.",
       ""]

for tag in sorted(groups):
    ops = groups[tag]
    out.append(f"## {tag} ({len(ops)})")
    out.append("")
    out.append("| Method | Path | Description |")
    out.append("|--------|------|-------------|")
    for method, path, summary, first in ops:
        text = (summary or first).replace("|", "\\|")
        out.append(f"| `{method}` | `{path}` | {text} |")
    out.append("")

dest = os.path.join(BACKEND, "..", "docs", "API.md")
with open(dest, "w") as f:
    f.write("\n".join(out) + "\n")
print(f"wrote {os.path.abspath(dest)} — {sum(len(v) for v in groups.values())} operations, {len(groups)} groups")
