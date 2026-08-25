#!/usr/bin/env bash
# Is this bridge actually doing its job? Non-zero if not.
#
# The verdict lives in scripts/health_check.py so it can be tested; see
# that file for why (the old inline version read a key /api/health has
# never returned, so it only ever checked that the roster poll worked).
#
# Exit codes: 2 bridge unwell · 3 camera unhealthy · 4 stale/no frames
#             5 cameras configured but the vendor produced nothing
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
curl --fail --silent --show-error \
  "http://127.0.0.1:${BRIDGE_PORT:-8090}/api/health" \
  | python3 "$HERE/health_check.py"
