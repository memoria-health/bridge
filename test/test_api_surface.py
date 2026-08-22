"""The HTTP surface from the outside, in the posture it is deployed in.

Hosted means behind a proxy, and behind a proxy every request arrives
with a loopback peer address. These tests speak over a real socket with
loopback trust off, which is exactly what an internet request through
nginx looks like from inside this process.
"""
from __future__ import annotations

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from core import auth, store
from core.server import Bridge, create_app
from vendors.warner import WarnerVendor

CAMERA = {"camera_id": "warner_a", "device_id": "d1", "device_key": "p:d1:s",
          "name": "Den", "camera_user": "admin", "camera_password": "hunter2",
          "channel": 0, "stream_id": 1, "livekit_identity": "cam_1"}

SETTINGS = {"backend_url": "https://backend", "gateway_token": "T" * 43,
            "instance_id": "bridge-test", "livekit_url": "wss://lk",
            "poll_seconds": 3, "stream_mode": "continuous"}


@pytest.fixture
async def client(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "INVENTORY_PATH", tmp_path / "inventory.json")
    store.write_inventory({"version": 1, "cameras": [CAMERA]})
    monkeypatch.setattr(auth, "API_KEY", "s3cret")
    monkeypatch.setattr(auth, "TRUST_LOOPBACK", False)
    auth._capabilities.clear()
    app = await create_app(Bridge(SETTINGS, WarnerVendor()))
    # `unsafe` only because the test server is on an IP address rather
    # than a hostname; a real browser stores these without it.
    async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)) as c:
        yield c


# ── the administrative surface ────────────────────────────────────
async def test_the_camera_inventory_is_not_public(client):
    """The third bug. `/api/cameras` had no check at all, unlike every
    other route, and it lists camera_id, device_id, name and
    livekit_identity for every customer this bridge serves."""
    assert (await client.get("/api/cameras")).status == 403


async def test_the_inventory_opens_for_the_operator(client):
    response = await client.get("/api/cameras", headers={"x-bridge-key": "s3cret"})
    assert response.status == 200
    assert (await response.json())["cameras"][0]["camera_id"] == "warner_a"


async def test_provisioning_is_not_public(client):
    """Open, this makes an email plus network reach enough to attach a
    camera to a stranger's account."""
    response = await client.post("/api/provision", json={"email": "a@b.com"})
    assert response.status == 403


async def test_the_admin_page_is_not_public(client):
    assert (await client.get("/")).status == 403


async def test_the_vendor_sdk_mount_is_not_public(client):
    """A static mount has no handler of ours to put a check in, so it was
    the one surface serving its whole directory to anyone."""
    assert (await client.get("/sdk/play.js")).status == 403


async def test_the_key_in_the_url_opens_the_page_and_its_assets(client):
    """A human cannot put a header on a navigation. The key arrives once
    in the URL and comes back as a cookie, which is the only credential
    an <iframe> or a <script src> carries by itself."""
    assert (await client.get("/", params={"key": "s3cret"})).status == 200
    assert (await client.get("/sdk/index.html")).status == 200


# ── the machine surface the local page needs ──────────────────────
async def test_the_local_page_works_with_no_operator_key(client):
    """The constraint that makes this hard: our own headless page cannot
    send a header, and a browser cannot set one on a WebSocket handshake
    at all. Its capability arrives in the URL the supervisor navigates
    to, and every request after that carries the cookie."""
    token = auth.grant("warner_a")
    page = await client.get(
        "/runtime", params={"camera_id": "warner_a", "cap": token})
    assert page.status == 200

    assert (await client.get("/sdk/play.js")).status == 200
    config = await client.get("/api/runtime/warner_a/config")
    assert config.status == 200
    assert (await config.json())["camera_password"] == "hunter2"
    assert (await client.post("/api/runtime/warner_a/status",
                              json={"state": "camera_connected"})).status == 200


async def test_a_page_cannot_read_another_camera(client):
    """One customer's page must not read another customer's camera
    password out of /config."""
    store.add({**CAMERA, "camera_id": "warner_b", "device_id": "d2"})
    token = auth.grant("warner_a")
    auth.grant("warner_b")
    await client.get("/runtime", params={"camera_id": "warner_a", "cap": token})
    assert (await client.get("/api/runtime/warner_b/config")).status == 403


async def test_a_page_cannot_reach_the_administrative_surface(client):
    token = auth.grant("warner_a")
    await client.get("/runtime", params={"camera_id": "warner_a", "cap": token})
    assert (await client.get("/api/cameras")).status == 403
    response = await client.post("/api/provision", json={"email": "a@b.com"})
    assert response.status == 403


async def test_a_page_url_without_a_capability_is_refused(client):
    """Otherwise the whole scheme is a query parameter anyone can omit."""
    assert (await client.get(
        "/runtime", params={"camera_id": "warner_a"})).status == 403


# ── unchanged on purpose ──────────────────────────────────────────
async def test_health_still_answers_a_monitoring_probe(client):
    """Liveness for anyone, inventory only for an authorized caller — the
    shape the rest of the surface now matches."""
    response = await client.get("/api/health")
    assert response.status == 200
    body = await response.json()
    assert body["configured_cameras"] == 1
    assert "stream_policy" not in body, "room names are patient_<uuid>"
