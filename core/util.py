#!/usr/bin/env python3
"""Small shared helpers. No vendor knowledge, no LiveKit knowledge."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def redact(message: str) -> str:
    """Strip credentials that travel in a URL out of anything we log.

    `cap` and `key` are here because a browser cannot carry a header, so
    the bridge's own credentials arrive in query strings — and a page
    that fails to load reports the URL it was opening.
    """
    return re.sub(
        r"([?&](?:access_token|join_request|cap|key)=)[^&\s}]+",
        r"\1<redacted>", message)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write 0600 via a temp file + rename, so a crash cannot truncate
    an inventory that holds camera credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def post_json(url: str, body: dict[str, Any], headers=None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode())


def get_json(url: str, headers=None, timeout=10) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())
