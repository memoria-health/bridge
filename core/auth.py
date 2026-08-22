#!/usr/bin/env python3
"""Who may call the bridge API.

Two surfaces, two credentials, because they have two callers.

The ADMINISTRATIVE surface — the provisioning page, the inventory, the
SDK assets — is used by a human and needs the operator key. The RUNTIME
surface is used by our own headless page, which cannot send a header:
a browser cannot set one on a `<script src>`, an `<iframe>` or a
WebSocket handshake. That constraint is why loopback was trusted
outright, and why the fix is not "require the key everywhere".

So each caller gets a credential it can actually carry. The page is
navigated to a URL holding a capability good for ONE camera; the
operator opens the admin page with `?key=`. Whatever got you in is
handed straight back as a cookie, which is the one thing a browser
attaches to every subresource, iframe and socket by itself.

TRADEOFF: a cookie is ambient, so it rides along on any request the
browser makes to this origin. SameSite=Strict is what keeps another
site from steering it, and the capability being per-camera and dying
with the page is what bounds the damage if one leaks. The alternative
considered was binding admin and runtime to different interfaces, which
needs no cookie but cannot survive `docker run -p` pointing at the
wrong one.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets

from aiohttp import web

from core.config import ALLOW_TAILNET, API_KEY, TAILNET_CIDR, TRUST_LOOPBACK

LOOPBACK = {"127.0.0.1", "::1"}

GRANT_COOKIE = "bridge_grant"
KEY_PARAM = "key"
CAP_PARAM = "cap"

# camera_id -> the one-camera capability its page holds. In memory only:
# a grant must not outlive the page it was minted for, and the stall
# watchdog recreates pages all day.
_capabilities: dict[str, str] = {}


def grant(camera_id: str) -> str:
    """Mint the credential this camera's page will carry. Replaces any
    older one, so a recreated page invalidates the page it replaced."""
    _capabilities[camera_id] = secrets.token_urlsafe(32)
    return _capabilities[camera_id]


def revoke(camera_id: str) -> None:
    _capabilities.pop(camera_id, None)


def hand_back(request: web.Request, response: web.StreamResponse
              ) -> web.StreamResponse:
    """Return the credential the caller arrived with, as a cookie.

    Not `Secure`: the headless page reaches us over plain http on
    loopback, and a Secure cookie would simply never come back.
    """
    presented = _presented(request)
    if presented:
        response.set_cookie(GRANT_COOKIE, presented, httponly=True,
                            samesite="Strict", path="/")
    return response


def _presented(request: web.Request) -> str:
    """The credential from the places a browser can put one.

    URL before cookie, so a stale cookie cannot shadow the credential
    someone just deliberately typed and lock them out of their own page.
    """
    return (request.query.get(CAP_PARAM)
            or request.query.get(KEY_PARAM)
            or request.cookies.get(GRANT_COOKIE)
            or "")


def _is_admin(request: web.Request) -> bool:
    supplied = request.headers.get("x-bridge-key", "") or _presented(request)
    return bool(API_KEY) and hmac.compare_digest(supplied, API_KEY)


def _holds(request: web.Request, camera_id: str) -> bool:
    wanted = _capabilities.get(camera_id, "")
    return bool(wanted) and hmac.compare_digest(_presented(request), wanted)


def _on_box(request: web.Request) -> bool:
    return TRUST_LOOPBACK and request.remote in LOOPBACK


def _on_tailnet(request: web.Request) -> bool:
    """Tailscale hands every node a 100.64.0.0/10 address. A SECOND
    factor beside the key, never authentication on its own: a source
    address can be spoofed, a shared secret cannot be guessed. So it
    only counts where there is no key to be a first factor."""
    if API_KEY or not ALLOW_TAILNET or request.remote is None:
        return False
    try:
        return ipaddress.ip_address(request.remote) in TAILNET_CIDR
    except ValueError:
        return False


def _refuse(request: web.Request, what: str) -> None:
    """Every path below ends here, so the reason is written once. `None`
    is not a trusted address; it is the absence of one."""
    if request.remote is None:
        raise web.HTTPForbidden(text="unidentifiable peer")
    raise web.HTTPForbidden(text=f"{what} surface: bad or missing credential")


def require_admin(request: web.Request) -> None:
    """The human surface: the admin page, provisioning, the inventory.

    The original trusted `request.remote` alone and treated `None` as
    trusted, which is backwards; then it trusted loopback outright,
    which a hosted service behind a proxy cannot afford.
    """
    if _is_admin(request) or _on_box(request) or _on_tailnet(request):
        return
    _refuse(request, "admin")


def require_runtime(request: web.Request, camera_id: str) -> None:
    """The machine surface: what our own page calls about ONE camera.

    Scoped per camera because this bridge serves several customers at
    once — one page holding a token for all of them would be one browser
    compromise away from every camera password on the host.
    """
    if (_holds(request, camera_id) or _is_admin(request)
            or _on_box(request) or _on_tailnet(request)):
        return
    _refuse(request, "runtime")


def require_asset(request: web.Request) -> None:
    """Static files, which have no handler of ours to check in.

    A grant of EITHER kind is enough: the admin page frames
    /sdk/index.html and the runtime page pulls a dozen scripts out of
    the same directory, so the mount cannot belong to one surface. It
    serves a vendor bundle rather than customer data; the point is that
    a stranger cannot walk it.
    """
    if _is_admin(request) or _on_box(request) or _on_tailnet(request):
        return
    presented = _presented(request)
    if presented and any(hmac.compare_digest(presented, token)
                         for token in _capabilities.values()):
        return
    _refuse(request, "asset")


def require_loopback(request: web.Request, camera_id: str) -> None:
    """For responses carrying camera credentials. A leaked API key must
    not also mean leaked camera logins, and a proxied request must not
    be able to claim the page's place."""
    require_runtime(request, camera_id)
    if request.remote not in LOOPBACK:
        raise web.HTTPForbidden(text="loopback-only (carries credentials)")
