#!/usr/bin/env python3
"""Everything read from the environment or from disk, in one place.

Kept vendor-neutral on purpose: a vendor may add its own env, but the
backend URL, the gateway token, the LiveKit credentials and the network
posture are the bridge's, not any camera brand's.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any

from core.identity import load_identity
from core.util import load_json, log

HERE = Path(__file__).resolve().parent.parent

INVENTORY_PATH = Path(os.environ.get("BRIDGE_INVENTORY", HERE / "inventory.json"))
GATEWAY_CONFIG_PATH = Path(os.environ.get("BRIDGE_GATEWAY_CONFIG", HERE / "cameras.json"))

# Loopback by DEFAULT. This used to be 0.0.0.0 with only a peer-address
# check in front of it, which is one `docker run -p` away from being on
# the internet. Widen deliberately, and pair it with BRIDGE_KEY.
HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "8090"))
API_KEY = os.environ.get("BRIDGE_KEY", "")

# Tailscale hands every node a 100.64.0.0/10 address. A SECOND factor
# beside the key, never authentication on its own: a source address can
# be spoofed, a shared secret cannot be guessed.
TAILNET_CIDR = ipaddress.ip_network("100.64.0.0/10")
ALLOW_TAILNET = os.environ.get("BRIDGE_ALLOW_TAILNET", "") == "1"
PUBLIC_ORIGIN = os.environ.get("BRIDGE_ORIGIN", f"http://127.0.0.1:{PORT}")

# A camera that never EOFs can stall silently, so absence of frames is
# the only honest liveness signal we have.
FRAME_STALL_SECONDS = float(os.environ.get("BRIDGE_FRAME_STALL_SECONDS", "20"))
FRAME_STARTUP_GRACE_SECONDS = float(
    os.environ.get("BRIDGE_FRAME_STARTUP_GRACE_SECONDS", "45"))

VENDOR = os.environ.get("BRIDGE_VENDOR", "warner")

# Reported on every poll so the backend can tell which bridges have a
# fix and which have not. Read from the file the release process bumps.
try:
    VERSION = (HERE / "VERSION.txt").read_text(encoding="utf-8").strip()
except OSError:
    VERSION = "unknown"


def _first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def load_settings() -> dict[str, Any]:
    cfg = load_json(GATEWAY_CONFIG_PATH, {})
    identity = load_identity()
    if cfg.get("livekit_api_secret"):
        log("IGNORING livekit_api_secret in cameras.json: the backend mints "
            "every token now. Delete the key — it is a project-wide signing "
            "secret and nothing here needs it.")
    if cfg.get("gateway_token"):
        # Loud, because a copied cameras.json is exactly the shared
        # credential this replaced. Delete the key from the file.
        log(f"IGNORING gateway_token in {GATEWAY_CONFIG_PATH.name}: this bridge "
            f"uses its own minted token. Remove the key.")
    return {
        "instance_id": identity["instance_id"],
        "backend_url": _first("BACKEND_URL") or cfg.get("backend_url", ""),
        "gateway_token": identity["token"],
        "livekit_url": _first("LIVEKIT_URL") or cfg.get("livekit_url", ""),
        # NOT the LiveKit API key/secret. The backend mints every token
        # now, so no bridge host holds a project-wide signing secret. A
        # value left in cameras.json is ignored; delete it.
        "poll_seconds": float(cfg.get("poll_seconds", 3)),
        # Cameras publish continuously so the AI can subscribe while the
        # caregiver's app is closed. "policy" only once the backend gives
        # a combined viewer/AI desired state we trust.
        "stream_mode": str(cfg.get("stream_mode", "continuous")).lower(),
    }


def validate_settings(settings: dict[str, Any]) -> None:
    missing = [key for key in (
        "backend_url", "gateway_token", "livekit_url",
    ) if not settings.get(key)]
    if missing:
        raise RuntimeError(f"missing gateway settings: {', '.join(missing)}")
