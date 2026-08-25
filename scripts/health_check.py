#!/usr/bin/env python3
"""Decide whether this bridge is actually doing its job. Reads
/api/health JSON on stdin; exit code is the verdict.

    2  the bridge itself is unwell (roster unreachable, preflight failed)
    3  a camera is unhealthy
    4  a camera has stale frames, or none yet
    5  cameras are configured but the vendor produced no entity at all

It used to live inline in health.sh and read `data["workers"]` — a key
/api/health has never returned in this shape. The body carries
`entities`, a LIST. `.get("workers", {})` silently yielded {}, so the
loop never ran and the only surviving check was `ok`, which reports
whether the last roster poll succeeded. A bridge whose Chromium never
started polls the roster perfectly well, so it exited 0.

That is the failure this service keeps producing — answering, polling,
looking healthy, publishing nothing — reproduced in the tool an operator
reaches for during an incident. A separate file so it can be tested.
"""
from __future__ import annotations

import json
import os
import sys


def verdict(data: dict, stall_seconds: float = 20.0) -> tuple[int, str]:
    """(exit_code, message). 0 means healthy."""
    if not data.get("ok"):
        return 2, "bridge reports not ok (roster unreachable?)"

    entities = data.get("entities") or []
    configured = data.get("configured_cameras") or 0

    # The missing-Chromium shape: roster fine, cameras configured, and
    # the vendor produced nothing. Everything else looks healthy.
    if configured and not entities:
        return 5, (f"no vendor entities for {configured} configured "
                   f"camera(s) — the vendor never started")

    for e in entities:
        cam = e.get("id", "?")
        # `healthy` is computed server-side from the page AND the
        # publisher. Trust it rather than re-deriving a state machine
        # here and drifting from it again, which is how this broke.
        if not e.get("healthy"):
            return 3, (f"unhealthy: {cam} state={e.get('state')} "
                       f"detail={e.get('detail')}")
        age = e.get("last_frame_age_s")
        if age is None:
            return 4, f"no frames yet: {cam}"
        if float(age) > stall_seconds:
            return 4, f"stale video: {cam} age={age}s"

    return 0, "ok"


def main() -> int:
    data = json.load(sys.stdin)
    print(json.dumps(data, indent=2))
    code, message = verdict(
        data, float(os.environ.get("BRIDGE_STALL_SECONDS", "20")))
    if code:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
