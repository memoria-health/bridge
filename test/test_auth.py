"""Who may call the bridge, and the three ways that has been wrong."""
from __future__ import annotations

import pytest
from aiohttp import web

from core import auth, config


class FakeRequest:
    def __init__(self, remote, headers=None, query=None, cookies=None):
        self.remote = remote
        self.headers = headers or {}
        # The two things a browser CAN carry where it cannot carry a
        # header: a query string it was navigated to, and a cookie.
        self.query = query or {}
        self.cookies = cookies or {}


@pytest.fixture(autouse=True)
def no_leftover_grants():
    """Capabilities are process state, so one test's page must not still
    be authorized in the next."""
    auth._capabilities.clear()
    yield
    auth._capabilities.clear()


def with_key(monkeypatch, key="s3cret", tailnet=False):
    monkeypatch.setattr(auth, "API_KEY", key)
    monkeypatch.setattr(auth, "ALLOW_TAILNET", tailnet)


def hosted(monkeypatch, key="s3cret"):
    """The posture this service is actually deployed in. Behind nginx, an
    ALB or `docker run -p`, EVERY request arrives with a loopback peer
    address, so loopback stops being proof of anything."""
    with_key(monkeypatch, key=key)
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)


def on_box(monkeypatch, key="s3cret"):
    with_key(monkeypatch, key=key)
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", True)


def test_our_own_page_is_allowed_even_when_a_key_is_set(monkeypatch):
    """The first bug. The key was checked BEFORE the loopback shortcut,
    and our headless page cannot send a custom header — a browser cannot
    set one on a WebSocket handshake at all. So setting BRIDGE_KEY, which
    the deploy unit tells operators to do for tailnet access, took every
    camera on the box dark."""
    on_box(monkeypatch)
    auth.require_runtime(FakeRequest("127.0.0.1"), "warner_a")
    auth.require_runtime(FakeRequest("::1"), "warner_a")


def test_a_remote_caller_still_needs_the_key(monkeypatch):
    with_key(monkeypatch)
    with pytest.raises(web.HTTPForbidden):
        auth.require_admin(FakeRequest("100.71.4.9"))
    auth.require_admin(FakeRequest("100.71.4.9", {"x-bridge-key": "s3cret"}))


def test_a_wrong_key_is_refused(monkeypatch):
    with_key(monkeypatch)
    with pytest.raises(web.HTTPForbidden):
        auth.require_admin(FakeRequest("100.71.4.9", {"x-bridge-key": "nope"}))


def test_an_unknown_peer_is_never_trusted(monkeypatch):
    """None is not a trusted address; it is the absence of one."""
    with_key(monkeypatch, key="")
    with pytest.raises(web.HTTPForbidden):
        auth.require_admin(FakeRequest(None))


def test_without_a_key_the_bridge_is_loopback_only(monkeypatch):
    on_box(monkeypatch, key="")
    auth.require_admin(FakeRequest("127.0.0.1"))
    with pytest.raises(web.HTTPForbidden):
        auth.require_admin(FakeRequest("100.71.4.9"))


def test_the_tailnet_is_a_second_factor_not_a_first(monkeypatch):
    """Reachable over the mesh is 'where you are'. On its own it is not
    authentication, because a source address can be spoofed."""
    with_key(monkeypatch, key="s3cret", tailnet=True)
    with pytest.raises(web.HTTPForbidden):
        auth.require_admin(FakeRequest("100.71.4.9"))


def test_credential_bearing_routes_stay_loopback_even_with_a_valid_key(monkeypatch):
    """A leaked API key must not also mean leaked camera logins."""
    on_box(monkeypatch)
    with pytest.raises(web.HTTPForbidden):
        auth.require_loopback(
            FakeRequest("100.71.4.9", {"x-bridge-key": "s3cret"}), "warner_a")
    auth.require_loopback(FakeRequest("127.0.0.1"), "warner_a")


# ── a hosted service cannot trust a loopback peer ─────────────────
def test_a_proxied_stranger_is_not_this_box(monkeypatch):
    """The second bug, and the expensive one. Loopback was treated as
    fully authorized with NO credential, defended by a comment saying the
    service must not be fronted by a reverse proxy — which a hosted
    service cannot honour. Behind nginx or an ALB the whole API,
    provisioning included, was open to the internet."""
    hosted(monkeypatch)
    with pytest.raises(web.HTTPForbidden):
        auth.require_admin(FakeRequest("127.0.0.1"))


def test_binding_wide_is_what_stops_loopback_meaning_this_box():
    """`docker run -p` and an ALB target both need the process bound off
    loopback, which is the moment the peer address stops proving
    anything. An operator terminating a proxy on the same host has to say
    so with BRIDGE_TRUST_LOOPBACK=0."""
    assert config.bound_to_loopback("127.0.0.1") is True
    assert config.bound_to_loopback("localhost") is True
    assert config.bound_to_loopback("::1") is True
    assert config.bound_to_loopback("0.0.0.0") is False
    assert config.bound_to_loopback("10.0.0.245") is False


def test_a_browser_can_carry_the_key_where_it_cannot_carry_a_header(monkeypatch):
    """A human cannot put a header on a navigation either. The key
    arrives once in the URL and comes back as a cookie, which is the one
    credential an <iframe> and a <script src> carry by themselves."""
    hosted(monkeypatch)
    auth.require_admin(FakeRequest("127.0.0.1", query={auth.KEY_PARAM: "s3cret"}))
    auth.require_admin(
        FakeRequest("127.0.0.1", cookies={auth.GRANT_COOKIE: "s3cret"}))


# ── the local page keeps working without the operator key ─────────
def test_the_page_holds_a_capability_for_its_own_camera(monkeypatch):
    hosted(monkeypatch)
    token = auth.grant("warner_a")
    with pytest.raises(web.HTTPForbidden):
        auth.require_runtime(FakeRequest("127.0.0.1"), "warner_a")
    auth.require_runtime(
        FakeRequest("127.0.0.1", query={auth.CAP_PARAM: token}), "warner_a")
    auth.require_runtime(
        FakeRequest("127.0.0.1", cookies={auth.GRANT_COOKIE: token}), "warner_a")


def test_a_capability_is_good_for_one_camera_only(monkeypatch):
    """Otherwise one customer's page reads another customer's camera
    password out of /config."""
    hosted(monkeypatch)
    token = auth.grant("warner_a")
    auth.grant("warner_b")
    with pytest.raises(web.HTTPForbidden):
        auth.require_runtime(
            FakeRequest("127.0.0.1", query={auth.CAP_PARAM: token}), "warner_b")


def test_a_torn_down_page_loses_its_capability(monkeypatch):
    """The stall watchdog recreates pages all day. A grant must not
    outlive the page it was minted for."""
    hosted(monkeypatch)
    token = auth.grant("warner_a")
    auth.revoke("warner_a")
    with pytest.raises(web.HTTPForbidden):
        auth.require_runtime(
            FakeRequest("127.0.0.1", query={auth.CAP_PARAM: token}), "warner_a")


def test_a_capability_does_not_open_the_admin_surface(monkeypatch):
    """A page is a machine caller. It must not be able to provision a
    camera onto a customer account."""
    hosted(monkeypatch)
    token = auth.grant("warner_a")
    with pytest.raises(web.HTTPForbidden):
        auth.require_admin(FakeRequest("127.0.0.1", query={auth.CAP_PARAM: token}))


def test_the_static_mount_takes_a_grant_of_either_kind(monkeypatch):
    """Both surfaces load from /sdk: the admin page frames index.html and
    the runtime page pulls a dozen scripts out of the same directory."""
    hosted(monkeypatch)
    with pytest.raises(web.HTTPForbidden):
        auth.require_asset(FakeRequest("127.0.0.1"))
    token = auth.grant("warner_a")
    auth.require_asset(FakeRequest("127.0.0.1", cookies={auth.GRANT_COOKIE: token}))
    auth.require_asset(FakeRequest("127.0.0.1", {"x-bridge-key": "s3cret"}))


def test_a_bridge_bound_to_loopback_is_unchanged(monkeypatch):
    """A dev machine or a Pi with nothing in front of it must not
    suddenly need a key to run its own cameras."""
    on_box(monkeypatch, key="")
    auth.require_admin(FakeRequest("127.0.0.1"))
    auth.require_runtime(FakeRequest("127.0.0.1"), "warner_a")
    auth.require_asset(FakeRequest("127.0.0.1"))
    auth.require_loopback(FakeRequest("127.0.0.1"), "warner_a")


# ── the bridge holds no signing secret ────────────────────────────
def test_the_bridge_requires_no_livekit_credentials(monkeypatch, tmp_path):
    """Signing our own tokens meant every bridge host carried the
    project-wide LiveKit secret, which mints a token for ANY room — that
    is, any patient's live video — on a machine whose whole job is
    talking to a camera vendor. The backend mints them now."""
    monkeypatch.setenv("BRIDGE_TOKEN", "ops-token")
    settings = {"backend_url": "https://b", "gateway_token": "t",
                "livekit_url": "wss://lk"}
    config.validate_settings(settings)      # must not raise


def test_a_livekit_secret_left_in_the_config_is_not_used(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "ops-token")
    cfg = tmp_path / "cameras.json"
    cfg.write_text('{"backend_url":"https://b","livekit_url":"wss://lk",'
                   '"livekit_api_secret":"STALE-SECRET-DO-NOT-USE"}')
    monkeypatch.setattr(config, "GATEWAY_CONFIG_PATH", cfg)
    settings = config.load_settings()
    assert "livekit_api_secret" not in settings
    assert "STALE-SECRET-DO-NOT-USE" not in repr(settings)
