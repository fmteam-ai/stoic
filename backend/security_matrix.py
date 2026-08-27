"""Repository-wide BOLA authorization matrix.

Every API route that accepts a tenant-sensitive object reference
(account_id / bot_id) MUST be declared here together with its ownership
enforcement mechanism. tests/test_iter211_corrections.py enumerates the
live OpenAPI schema and FAILS when a sensitive route is missing from
this matrix — so new endpoints cannot ship without an explicit
authorization decision (the recurring-BOLA class dies here).

Mechanisms:
  user_scoped_query    — Mongo query includes {"user_id": caller}
  owned_account_helper — _owned_account() 404s on non-owned (admin bypass)
"""

USQ = "user_scoped_query"
OAH = "owned_account_helper"

BOLA_MATRIX: dict[tuple[str, str], str] = {
    # accounts
    ("PATCH", "/api/accounts/{account_id}"): USQ,
    ("DELETE", "/api/accounts/{account_id}"): USQ,
    ("GET", "/api/accounts/{account_id}/block-status"): USQ,
    ("POST", "/api/accounts/{account_id}/certify"): USQ,
    ("PATCH", "/api/accounts/{account_id}/credentials"): USQ,
    ("POST", "/api/accounts/{account_id}/credentials/reveal"): USQ,
    ("POST", "/api/accounts/{account_id}/import-positions"): USQ,
    ("POST", "/api/accounts/{account_id}/request-sync"): USQ,
    ("POST", "/api/accounts/{account_id}/rotate-token"): USQ,
    ("PUT", "/api/accounts/{account_id}/symbol-suffix"): USQ,
    ("GET", "/api/accounts/{account_id}/test-connection"): USQ,
    ("POST", "/api/accounts/{account_id}/test-trade"): USQ,
    ("POST", "/api/accounts/{account_id}/unblock"): USQ,
    # bot
    ("GET", "/api/bot/adaptive-status"): USQ,
    ("GET", "/api/bot/config"): USQ,
    ("PUT", "/api/bot/config"): USQ,
    ("DELETE", "/api/bot/config"): USQ,
    ("GET", "/api/bot/doctor"): USQ,
    ("POST", "/api/bot/my-presets"): USQ,
    ("POST", "/api/bot/preset/{key}"): USQ,
    ("GET", "/api/bot/sizing-preview"): USQ,
    ("POST", "/api/bot/start"): USQ,
    ("GET", "/api/bot/status"): USQ,
    ("POST", "/api/bot/stop"): USQ,
    # brain
    ("GET", "/api/brain/costs"): OAH,
    ("GET", "/api/brain/portfolio"): OAH,
    ("GET", "/api/brain/regime"): OAH,
    ("GET", "/api/brain/health"): OAH,
    # broker intel / config / crypto
    ("GET", "/api/broker-intel/forecast"): USQ,
    ("POST", "/api/config/rollback"): USQ,
    ("GET", "/api/config/versions"): USQ,
    ("DELETE", "/api/crypto/accounts/{account_id}"): USQ,
    ("GET", "/api/crypto/accounts/{account_id}/balance"): USQ,
    ("POST", "/api/crypto/accounts/{account_id}/execute"): USQ,
    ("GET", "/api/crypto/accounts/{account_id}/ticker"): USQ,
    ("POST", "/api/crypto/accounts/{account_id}/verify"): USQ,
    # execution / optimizer / portfolio / risk
    ("GET", "/api/execution/quality"): USQ,
    ("POST", "/api/optimizer/analyze"): USQ,
    ("GET", "/api/optimizer/report"): USQ,
    ("GET", "/api/portfolio/snapshot"): USQ,
    ("GET", "/api/risk/budget"): USQ,
    ("GET", "/api/risk/layers"): USQ,
    ("GET", "/api/risk/strategy-portfolio"): USQ,
    # scalp
    ("DELETE", "/api/scalp/config"): USQ,
    ("GET", "/api/scalp/executions"): USQ,
    ("GET", "/api/scalp/metrics"): USQ,
    ("POST", "/api/scalp/retrain"): USQ,
    ("GET", "/api/scalp/status"): USQ,
    # setup / signals / trades
    ("GET", "/api/setup/pairing-status/{account_id}"): USQ,
    ("POST", "/api/signals/generate"): USQ,
    ("POST", "/api/signals/generate-all"): USQ,
    ("GET", "/api/trades"): USQ,
    ("GET", "/api/trades/history"): USQ,
    ("GET", "/api/trades/live"): USQ,
    ("GET", "/api/trades/stats"): USQ,
    ("GET", "/api/v1/trades"): USQ,
}

SENSITIVE_PARAMS = {"account_id", "bot_id"}


def sensitive_routes_from_openapi(spec: dict) -> set[tuple[str, str]]:
    out = set()
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if not isinstance(op, dict):
                continue
            params = {p.get("name") for p in op.get("parameters", [])}
            if params & SENSITIVE_PARAMS or any(
                    "{%s}" % s in path for s in SENSITIVE_PARAMS):
                out.add((method.upper(), path))
    return out


def to_markdown() -> str:
    lines = ["# BOLA Authorization Matrix",
             "",
             "Auto-generated from `backend/security_matrix.py`. Every "
             "route accepting `account_id`/`bot_id` is declared with its "
             "ownership enforcement; the test suite fails when a "
             "sensitive route is missing.",
             "",
             "| Method | Path | Enforcement |",
             "|--------|------|-------------|"]
    for (method, path), mech in sorted(BOLA_MATRIX.items(),
                                       key=lambda x: x[0][1]):
        lines.append(f"| {method} | `{path}` | {mech} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(to_markdown())
