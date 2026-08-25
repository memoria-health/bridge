"""The installer console must never be served from a browser cache.

`web.FileResponse` sends ETag and Last-Modified and no Cache-Control, so
a plain refresh returns 304 and the browser keeps whatever copy it
already had. Observed 2026-08-23: a change shipped to the page, the
served bytes contained it, and a refresh kept showing the old console.

That is worse than a stale dev loop. This page is what an installer
drives in a customer's home, and a cached copy can carry fields we have
since removed, or lack the fix we shipped precisely because the old one
misled them.
"""
from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core import store
from core.server import Bridge, create_app
from vendors.warner import WarnerVendor

SETTINGS = {"backend_url": "https://backend", "gateway_token": "t",
            "instance_id": "bridge-test", "livekit_url": "wss://lk",
            "poll_seconds": 3, "stream_mode": "continuous"}


@pytest.fixture
async def client(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "INVENTORY_PATH", tmp_path / "inventory.json")
    app = await create_app(Bridge(SETTINGS, WarnerVendor()))
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.mark.asyncio
async def test_the_admin_page_forbids_caching(client):
    """THE regression."""
    r = await client.get("/")
    assert r.status == 200
    assert "no-store" in r.headers.get("Cache-Control", ""), (
        "without no-store a refresh 304s and the installer keeps an old "
        "console"
    )


@pytest.mark.asyncio
async def test_a_conditional_request_is_still_answered_in_full(client):
    """A browser honouring no-store will not send If-Modified-Since at
    all — but one that does must not be handed a 304."""
    first = await client.get("/")
    stamp = first.headers.get("Last-Modified")
    if stamp is None:
        pytest.skip("no validator sent; nothing to revalidate against")

    again = await client.get("/", headers={"If-Modified-Since": stamp})
    assert again.status == 200, (
        "answered 304, so a refreshing installer keeps the cached page"
    )
