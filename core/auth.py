#!/usr/bin/env python3
"""Who may call the bridge API."""
from __future__ import annotations

import hmac
import ipaddress

from aiohttp import web

from core.config import ALLOW_TAILNET, API_KEY, TAILNET_CIDR


def require_local(request: web.Request) -> None:
    """Authorize a call: loopback is this box, everything else needs proof.

    The original trusted `request.remote` alone, and treated `None`
    (unknown peer) as trusted, which is backwards. Note the standing
    caveat: behind a reverse proxy the TCP peer reads as the proxy's
    address, usually 127.0.0.1, so this service must NOT be fronted by
    one. It binds loopback by default for exactly that reason.
    """
    peer = request.remote
    if peer in {"127.0.0.1", "::1"}:
        # A genuine loopback peer is this box talking to itself, and it is
        # the ONLY caller of the runtime routes. Checking the key first
        # broke that: our own headless page cannot send a custom header,
        # and a browser cannot set one on a WebSocket handshake at all —
        # so setting BRIDGE_KEY, which deploy/memoria-bridge.service tells
        # operators to do for tailnet access, took every camera on the box
        # dark. The key exists to gate REMOTE access; loopback is not
        # remote.
        return
    if peer is None:
        raise web.HTTPForbidden(text="unidentifiable peer")

    if API_KEY:
        supplied = request.headers.get("x-bridge-key", "")
        if not hmac.compare_digest(supplied, API_KEY):
            raise web.HTTPForbidden(text="bad or missing x-bridge-key")
        # A valid key from off-box is allowed on purpose; that is what the
        # key is for.
        return
    if ALLOW_TAILNET:
        try:
            if ipaddress.ip_address(peer) in TAILNET_CIDR:
                return
        except ValueError:
            pass
    raise web.HTTPForbidden(text="bridge API is loopback-only")


def require_loopback(request: web.Request) -> None:
    """For responses carrying camera credentials. A leaked API key must
    not also mean leaked camera logins."""
    require_local(request)
    if request.remote not in {"127.0.0.1", "::1"}:
        raise web.HTTPForbidden(text="loopback-only (carries credentials)")
