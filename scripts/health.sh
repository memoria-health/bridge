#!/usr/bin/env bash
set -euo pipefail
curl --fail --silent --show-error http://127.0.0.1:8090/api/health |
python3 -c '
import json, sys
data = json.load(sys.stdin)
print(json.dumps(data, indent=2))
if not data.get("ok"):
    raise SystemExit(2)
for camera_id, worker in data.get("workers", {}).items():
    state = worker.get("state")
    if state not in {"publishing", "camera_connected", "livekit_connecting"}:
        print(f"unhealthy worker state: {camera_id}={state}", file=sys.stderr)
        raise SystemExit(3)
    age = worker.get("frame_age_seconds")
    if age is not None and float(age) > 20:
        print(f"stale video frames: {camera_id} age={age}s", file=sys.stderr)
        raise SystemExit(4)
'
