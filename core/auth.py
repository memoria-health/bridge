#!/usr/bin/env python3
"""Who may call the bridge API."""
from __future__ import annotations

import hmac
import ipaddress

from aiohttp import web

from core.config import ALLOW_TAILNET, API_KEY, TAILNET_CIDR


def require_local(request: web.Request) -> None:
    """Authorize a call. Secret first, network scope second.

    The original trusted `request.remote` alone. That is the TCP peer, so
    behind ANY reverse proxy it reads as the proxy's address — usually
    127.0.0.1 — and the check silently became allow-all, precisely when
    the service was hosted somewhere reachable. It also treated `None`
    (unknown peer) as trusted, which is backwards.
    """
    if API_KEY:
        supplied = request.headers.get("x-bridge-key", "")
        if not hmac.compare_digest(supplied, API_KEY):
            raise web.HTTPForbidden(text="bad or missing x-bridge-key")

    peer = request.remote
    if peer in {"127.0.0.1", "::1"}:
        return
    if peer is None:
        raise web.HTTPForbidden(text="unidentifiable peer")
    if ALLOW_TAILNET:
        try:
            if ipaddress.ip_address(peer) in TAILNET_CIDR:
                return
        except ValueError:
            pass
    if API_KEY:
        # A valid key from off-box is allowed on purpose; that is what the
        # key is for. The peer check just means an UNKEYED deployment can
        # never be reached remotely.
        return
    raise web.HTTPForbidden(text="bridge API is loopback-only")


def require_loopback(request: web.Request) -> None:
    """For responses carrying camera credentials. A leaked API key must
    not also mean leaked camera logins."""
    require_local(request)
    if request.remote not in {"127.0.0.1", "::1"}:
        raise web.HTTPForbidden(text="loopback-only (carries credentials)")
