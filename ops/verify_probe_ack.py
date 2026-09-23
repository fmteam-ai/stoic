"""Validate an edge-probe ingestion acknowledgement (synthetic-monitor P2).

    printf '%s' "$body" | python3 ops/verify_probe_ack.py <endpoint> <region> <cadence_s>

Exit 0 only when the server confirmed THIS sample: recorded or duplicate,
same endpoint, expected region and the region's declared cadence. Any other
answer is a TELEMETRY failure — distinct from a route failure.
"""
import json
import sys


def check(body: str, endpoint: str, region: str, cadence_s: int) -> list:
    try:
        d = json.loads(body or "")
    except ValueError:
        return [f"non-JSON ingestion response: {body[:120]!r}"]
    problems = []
    if not (d.get("recorded") is True or d.get("duplicate") is True):
        problems.append("sample neither recorded nor duplicate")
    if d.get("endpoint") != endpoint:
        problems.append(f"endpoint mismatch: {d.get('endpoint')!r} != {endpoint!r}")
    if d.get("region") != region:
        problems.append(f"region {d.get('region')!r} != {region!r}")
    if d.get("cadence_s") != cadence_s:
        problems.append(f"server cadence for {region} is {d.get('cadence_s')}, expected {cadence_s} "
                        f"(set EDGE_PROBE_CADENCE={region}={cadence_s})")
    return problems


if __name__ == "__main__":
    ep, region, cad = sys.argv[1], sys.argv[2], int(sys.argv[3])
    probs = check(sys.stdin.read(), ep, region, cad)
    for p in probs:
        print(p, file=sys.stderr)
    sys.exit(1 if probs else 0)
