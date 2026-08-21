"""Who may call the bridge, and the two ways that has been wrong."""
from __future__ import annotations

import pytest
from aiohttp import web

from core import auth


class FakeRequest:
    def __init__(self, remote, headers=None):
        self.remote = remote
        self.headers = headers or {}


def with_key(monkeypatch, key="s3cret", tailnet=False):
    monkeypatch.setattr(auth, "API_KEY", key)
    monkeypatch.setattr(auth, "ALLOW_TAILNET", tailnet)


def test_our_own_page_is_allowed_even_when_a_key_is_set(monkeypatch):
    """The bug this replaces. The key was checked BEFORE the loopback
    shortcut, and our headless page cannot send a custom header — a
    browser cannot set one on a WebSocket handshake at all. So setting
    BRIDGE_KEY, which the deploy unit tells operators to do for tailnet
    access, took every camera on the box dark."""
    with_key(monkeypatch)
    auth.require_local(FakeRequest("127.0.0.1"))
    auth.require_local(FakeRequest("::1"))


def test_a_remote_caller_still_needs_the_key(monkeypatch):
    with_key(monkeypatch)
    with pytest.raises(web.HTTPForbidden):
        auth.require_local(FakeRequest("100.71.4.9"))
    auth.require_local(FakeRequest("100.71.4.9", {"x-bridge-key": "s3cret"}))


def test_a_wrong_key_is_refused(monkeypatch):
    with_key(monkeypatch)
    with pytest.raises(web.HTTPForbidden):
        auth.require_local(FakeRequest("100.71.4.9", {"x-bridge-key": "nope"}))


def test_an_unknown_peer_is_never_trusted(monkeypatch):
    """None is not a trusted address; it is the absence of one."""
    with_key(monkeypatch, key="")
    with pytest.raises(web.HTTPForbidden):
        auth.require_local(FakeRequest(None))


def test_without_a_key_the_bridge_is_loopback_only(monkeypatch):
    with_key(monkeypatch, key="")
    auth.require_local(FakeRequest("127.0.0.1"))
    with pytest.raises(web.HTTPForbidden):
        auth.require_local(FakeRequest("100.71.4.9"))


def test_the_tailnet_is_a_second_factor_not_a_first(monkeypatch):
    """Reachable over the mesh is 'where you are'. On its own it is not
    authentication, because a source address can be spoofed."""
    with_key(monkeypatch, key="s3cret", tailnet=True)
    with pytest.raises(web.HTTPForbidden):
        auth.require_local(FakeRequest("100.71.4.9"))


def test_credential_bearing_routes_stay_loopback_even_with_a_valid_key(monkeypatch):
    """A leaked API key must not also mean leaked camera logins."""
    with_key(monkeypatch, key="s3cret")
    with pytest.raises(web.HTTPForbidden):
        auth.require_loopback(FakeRequest("100.71.4.9", {"x-bridge-key": "s3cret"}))
    auth.require_loopback(FakeRequest("127.0.0.1"))
