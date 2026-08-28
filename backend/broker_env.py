"""Explicit broker environment classification (iter-213 P0):

  LIVE  — real-money broker account
  DEMO  — broker demo server (practice money at a real broker)
  PAPER — STOIC-internal simulation, no broker at all

An explicit `broker_environment` field on the account always wins;
otherwise PAPER mode and demo-server naming are detected."""

ENVIRONMENTS = ("LIVE", "DEMO", "PAPER")
_DEMO_TOKENS = ("demo", "trial", "practice", "contest")


def broker_environment(account: dict) -> str:
    explicit = str(account.get("broker_environment") or "").upper()
    if explicit in ENVIRONMENTS:
        return explicit
    if account.get("mode") == "paper":
        return "PAPER"
    server = str(account.get("broker_server") or account.get("server")
                 or "").lower()
    if any(t in server for t in _DEMO_TOKENS):
        return "DEMO"
    return "LIVE"
