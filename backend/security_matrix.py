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
PGA = "program_access"        # PAMM require_program_access permission model
MGR = "manager_scoped"        # require_manager + scope filter
ADM = "admin_only"            # role==admin gate (403 otherwise)

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
    # ── iter-212 expansion: decision / execution / trade / signal ids ──
    ("GET", "/api/attribution/trades/{trade_id}"): USQ,
    ("GET", "/api/brain/decisions/{decision_id}"): USQ,
    ("GET", "/api/execution/intents"): MGR,
    ("GET", "/api/execution/intents/{intent_id}"): MGR,
    ("POST", "/api/infra/installations/{installation_id}/revoke"): USQ,
    ("POST", "/api/journal/{trade_id}/card"): USQ,
    ("PUT", "/api/journal/{trade_id}/card"): USQ,
    ("GET", "/api/journal/{trade_id}/card"): USQ,
    ("DELETE", "/api/journal/{trade_id}/card"): USQ,
    ("POST", "/api/journal/{trade_id}/share"): USQ,
    ("POST", "/api/ops/signals/{signal_id}/replay-validate"): ADM,
    ("GET", "/api/postmortem/{trade_id}"): USQ,
    ("POST", "/api/postmortem/{trade_id}/regenerate"): USQ,
    ("DELETE", "/api/signals/{signal_id}"): USQ,
    ("GET", "/api/trades/events"): USQ,
    ("POST", "/api/trades/execute/{signal_id}"): USQ,
    ("GET", "/api/trades/{trade_id}/audit"): USQ,
    ("POST", "/api/trades/{trade_id}/close"): USQ,
    ("GET", "/api/trades/{trade_id}/dna"): USQ,
    ("GET", "/api/trades/{trade_id}/explain"): USQ,
    ("GET", "/api/trades/{trade_id}/replay"): USQ,
    ("POST", "/api/trades/{trade_id}/revive"): USQ,
    ("GET", "/api/trades/{trade_id}/timeline"): USQ,
    ("GET", "/api/trades/{trade_id}/trace"): USQ,
    # ── iter-212 expansion: PAMM program / request ids ──
    ("GET", "/api/pamm/events"): MGR,
    ("POST", "/api/pamm/join-requests/{request_id}/{decision}"): PGA,
    ("POST", "/api/pamm/marketplace/{program_id}/join"): PGA,
    ("GET", "/api/pamm/programs/{program_id}"): PGA,
    ("GET", "/api/pamm/programs/{program_id}/allocations"): PGA,
    ("GET", "/api/pamm/programs/{program_id}/change-requests"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/change-requests"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/clear-emergency-stop"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/clear-risk-breach"): PGA,
    ("PUT", "/api/pamm/programs/{program_id}/drift-tolerance"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/emergency-stop"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/investors"): PGA,
    ("GET", "/api/pamm/programs/{program_id}/join-requests"): PGA,
    ("GET", "/api/pamm/programs/{program_id}/master"): PGA,
    ("GET", "/api/pamm/programs/{program_id}/nav"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/op-state"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/pause"): PGA,
    ("GET", "/api/pamm/programs/{program_id}/position-truth"): PGA,
    ("POST",
     "/api/pamm/programs/{program_id}/position-truth/acknowledge"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/position-truth/check"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/publish"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/reconcile"): PGA,
    ("GET", "/api/pamm/programs/{program_id}/reconciliation"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/resume"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/risk-check"): PGA,
    ("GET", "/api/pamm/programs/{program_id}/risk-limits"): PGA,
    ("PUT", "/api/pamm/programs/{program_id}/risk-limits"): PGA,
    ("GET", "/api/pamm/programs/{program_id}/risk-status"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/trade-verdict"): PGA,
    # ── iter-212 additions: certification + soak ──
    ("GET", "/api/certification/system"): OAH,
    ("GET", "/api/ops/broker-validation"): ADM,
    # ── v62.1 — PAMM Strategy Profiles ──
    ("GET", "/api/pamm/strategies/nitro-eligibility"): OAH,
    ("GET", "/api/pamm/programs/{program_id}/strategy"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/strategy"): PGA,
    ("PATCH", "/api/pamm/programs/{program_id}/strategy"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/strategy/validate"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/strategy/activate"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/strategy/suspend"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/strategy/change"): PGA,
    ("GET", "/api/pamm/programs/{program_id}/certification"): PGA,
    ("GET", "/api/pamm/programs/{program_id}/certification/evidence"): PGA,
    ("POST", "/api/pamm/programs/{program_id}/certification/start"): ADM,
    ("POST",
     "/api/pamm/programs/{program_id}/certification/checkpoint"): ADM,
    ("POST", "/api/pamm/programs/{program_id}/certification/evaluate"): ADM,
    ("POST", "/api/pamm/programs/{program_id}/certification/advance"): ADM,
    ("POST", "/api/pamm/programs/{program_id}/certification/revoke"): ADM,
    ("GET", "/api/pamm/programs/{program_id}/strategy-ownership"): PGA,
    # Security & Health Agent (SA1) — admin read-only
    ("GET", "/api/admin/security/status"): ADM,
    ("GET", "/api/admin/security/findings"): ADM,
    ("GET", "/api/admin/security/findings/{finding_id}"): ADM,
    ("GET", "/api/admin/security/check-runs"): ADM,
    # SA3 — actions log, reports, writes (step-up MFA + audit chain)
    ("GET", "/api/admin/security/actions"): ADM,
    ("POST", "/api/admin/security/findings/{finding_id}/status"): ADM,
    ("POST", "/api/admin/security/mode"): ADM,
    ("POST", "/api/admin/security/test-alert"): ADM,
    ("GET", "/api/admin/security/reports/{kind}"): ADM,
}

SENSITIVE_PARAMS = {"account_id", "bot_id",
                    # iter-212 expansion — decision/execution/trade/signal,
                    # PAMM program/request and installation identifiers
                    "decision_id", "intent_id", "execution_id",
                    "installation_id", "trade_id", "signal_id",
                    "program_id", "request_id", "investor_id",
                    "fund_id", "pamm_id", "allocation_id"}


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
