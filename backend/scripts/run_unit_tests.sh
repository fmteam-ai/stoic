#!/usr/bin/env bash
# Round 15 item 10 — CI job 1: PURE UNIT suite.
# No MongoDB, no live broker, no network. Fast and deterministic.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m pytest tests/unit/ -q -p no:cacheprovider "$@"
