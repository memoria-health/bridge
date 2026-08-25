#!/usr/bin/env python3
"""This bridge's own credential, minted here and never shared.

The backend already stores one `public.gateways` row per instance with
only a sha256 of its bearer token, and `provision-camera` creates that
row for any token hash it has not seen — its docstring calls it "the
box's UNIQUE token, generated on first boot". The Pi boxes do exactly
that. The bridge did not: it read a token out of `cameras.json`, so
every copy of that file was the same credential.

One shared secret means a leak is a fleet-wide leak and revoking one
deployment revokes all of them. Minting locally costs nothing and makes
revocation a single DELETE.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from core.util import load_json, log, write_json_atomic

IDENTITY_PATH = Path(os.environ.get(
    "BRIDGE_IDENTITY", Path(__file__).resolve().parent.parent / "identity.json"))

# 32 bytes -> 43 url-safe chars, matching the backend's mint_ingest_token.
_TOKEN_BYTES = 32


def fingerprint(token: str) -> str:
    """Enough to match a token against a row, never enough to use it."""
    return f"{token[:6]}…{token[-4:]} (len {len(token)})" if token else "<none>"


def load_identity(path: Path | None = None) -> dict[str, Any]:
    """Return this instance's `{instance_id, token}`, minting once.

    `BRIDGE_TOKEN` overrides for ops and is deliberately NOT persisted:
    an environment override should leave no trace on disk to go stale.
    """
    path = path or IDENTITY_PATH
    override = os.environ.get("BRIDGE_TOKEN", "").strip()
    if override:
        return {"instance_id": os.environ.get("BRIDGE_INSTANCE_ID", "bridge-env"),
                "token": override, "source": "env"}

    data = load_json(path, {})
    if isinstance(data.get("token"), str) and data["token"]:
        return {**data, "source": "file"}

    identity = {
        "instance_id": f"bridge-{secrets.token_hex(4)}",
        "token": secrets.token_urlsafe(_TOKEN_BYTES),
    }
    write_json_atomic(path, identity)
    log(f"minted bridge identity {identity['instance_id']} "
        f"token {fingerprint(identity['token'])} -> {path}")
    # Until a camera is provisioned with this token the backend has no
    # row for it, so the roster will 401. That is the enrolment moment,
    # not an error.
    log("not enrolled yet: provision a camera to bind this bridge to an account")
    return {**identity, "source": "minted"}
