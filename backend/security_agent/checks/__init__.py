"""Check registry: id → (coroutine, interval_seconds). Event checks run every tick (events are rows)."""
from security_agent.checks import access as A, bridge_secrets_deps as B, integrity_platform_trading as I  # noqa: N812

MIN, HOUR = 60, 3600
CHECKS = {
    "A1": (A.A1, MIN), "A2": (A.A2, MIN), "A3": (A.A3, MIN), "A4": (A.A4, MIN), "A5": (A.A5, 5 * MIN), "A6": (A.A6, MIN), "A7": (A.A7, MIN),
    "B1": (B.B1, MIN), "B2": (B.B2, MIN), "B3": (B.B3, MIN),
    "S1": (B.S1, 5 * MIN), "S2": (B.S2, HOUR), "S3": (B.S3, 6 * HOUR), "S4": (B.S4, 6 * HOUR), "S5": (B.S5, 6 * HOUR),
    "D1": (B.D1, 24 * HOUR), "D2": (B.D2, 24 * HOUR),
    "I1": (I.I1, 15 * MIN), "I2": (I.I2, HOUR), "I3": (I.I3, HOUR), "I4": (I.I4, 6 * HOUR),
    "P1": (I.P1, MIN), "P2": (I.P2, MIN), "P3": (I.P3, MIN), "P4": (I.P4, 5 * MIN), "P5": (I.P5, 5 * MIN),
    "T1": (I.T1, MIN), "T2": (I.T2, MIN),
}
BOOT_CHECKS = ("S2", "I2")
