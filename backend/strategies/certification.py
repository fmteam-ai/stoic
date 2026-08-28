"""PAMM × Strategy certification identity + lifecycle (v62.1 skeleton,
full campaign machinery arrives in v62.2).

The certified production object is the COMBINATION
  (pamm_program_id, strategy_id, strategy_version, broker,
   broker_server, risk_profile, execution_environment)
— never just "Nitro"."""
import hashlib
import json

LIFECYCLE = ["DRAFT", "VALIDATING", "REPLAY", "SHADOW", "DEMO", "CANARY",
             "CERTIFIED", "LIVE", "SUSPENDED", "REVOKED"]

_TRANSITIONS = {
    "DRAFT": {"VALIDATING"},
    "VALIDATING": {"REPLAY", "DRAFT"},
    "REPLAY": {"SHADOW", "DRAFT"},
    "SHADOW": {"DEMO", "DRAFT"},
    "DEMO": {"CANARY", "DRAFT"},
    "CANARY": {"CERTIFIED", "DRAFT"},
    "CERTIFIED": {"LIVE", "SUSPENDED", "REVOKED"},
    "LIVE": {"SUSPENDED", "REVOKED"},
    "SUSPENDED": {"LIVE", "REVOKED"},
    "REVOKED": set(),
}

# any of these conditions AUTO-SUSPENDS a live certification
AUTO_SUSPEND_TRIGGERS = [
    "strategy_disabled", "strategy_decay_severe",
    "broker_certification_lost", "pamm_certification_lost",
    "position_truth_unhealthy", "execution_safety_degraded",
    "clock_unhealthy_for_nitro", "drawdown_hard_limit",
    "strategy_version_changed",
]


def cert_identity(pamm_program_id: str, strategy_id: str,
                  strategy_version: str, broker: str | None,
                  broker_server: str | None, risk_profile_id: str,
                  execution_environment: str) -> dict:
    ident = {"pamm_program_id": pamm_program_id,
             "strategy_id": strategy_id,
             "strategy_version": strategy_version,
             "broker": broker, "broker_server": broker_server,
             "risk_profile_id": risk_profile_id,
             "execution_environment": execution_environment}
    canonical = json.dumps(ident, sort_keys=True, separators=(",", ":"))
    ident["identity_hash"] = hashlib.sha256(
        canonical.encode()).hexdigest()[:32]
    return ident


def transition_allowed(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, set())
