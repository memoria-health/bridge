"""What this service tells the backend, and what proves the account.

A hosted bridge serves many customer accounts at once, and the backend
scopes a service's roster by gateway alone — the multi-tenant behaviour —
only when its `public.gateways` row says so. It sets that from the
`service_kind` on the call that binds it, and the bridge sent none. So its
row was created as a single-tenant BOX: provisioning for customer B
rebound the whole row to B, and every sensor of customer A dropped off the
roster and stopped publishing, silently.

The other half is WHAT proves the account. This sent the caregiver's
EMAIL, which is on their invoice and in their signature — so anyone who
knew one could bind this bridge to their home. It sends a five-minute
setup code the caregiver generated in the app instead, and binding and
provisioning are one call: `/v1/gateway/enroll` is gone, along with the
boot-time self-heal that existed only because binding used to be separate.
"""
from __future__ import annotations

import pytest

from core import server, store
from core.server import Bridge
from vendors.warner import WarnerVendor

PROVISION = "https://backend/v1/gateway/provision-camera"
TOKEN = "T" * 43
CODE = "482913"


class Calls(list):
    """Every backend POST, so a test can assert on the body AND on which
    endpoint received it."""

    raises = False

    def __call__(self, url, body, headers=None):
        if self.raises:
            raise RuntimeError("backend unreachable")
        self.append((url, body, headers or {}))
        return {"ok": True, "cameras": [{"livekit_identity": "cam_1"}]}

    def to(self, url: str) -> list[tuple]:
        return [call for call in self if call[0] == url]


class FakeRequest:
    """A loopback admin caller, which is what the provisioning page is."""

    def __init__(self, body):
        self.remote = "127.0.0.1"
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "INVENTORY_PATH", tmp_path / "inventory.json")
    calls = Calls()
    monkeypatch.setattr(server, "post_json", calls)
    instance = Bridge(
        {"backend_url": "https://backend", "gateway_token": TOKEN,
         "instance_id": "bridge-test", "livekit_url": "wss://lk",
         "poll_seconds": 3, "stream_mode": "continuous"},
        WarnerVendor())
    return instance, calls


async def provision(bridge, *, code=CODE, email=None, device="p:d1:s"):
    instance, _ = bridge
    body = {"name": "Den", "device_key": device}
    if code is not None:
        body["pairing_code"] = code
    if email is not None:
        body["email"] = email
    return await instance.provision(FakeRequest(body))


@pytest.mark.asyncio
async def test_provisioning_carries_the_setup_code(bridge):
    """The code is what binds this bridge to a customer's account."""
    _instance, calls = bridge
    await provision(bridge)

    sent = calls.to(PROVISION)
    assert len(sent) == 1
    assert sent[0][1]["pairing_code"] == CODE


@pytest.mark.asyncio
async def test_provisioning_declares_the_service_kind(bridge):
    """Without it the row is born a single-tenant box and the next
    customer's provision takes this one's sensors off the roster."""
    _instance, calls = bridge
    await provision(bridge)
    assert calls.to(PROVISION)[0][1]["service_kind"] == "camera-bridge"


@pytest.mark.asyncio
async def test_no_code_never_reaches_the_backend(bridge):
    """Refused here, not merely refused there.

    An installer who has not been given a code should be told so by the
    page in front of them, and a request that cannot succeed should not
    be sent at all.
    """
    _instance, calls = bridge
    with pytest.raises(Exception):
        await provision(bridge, code=None)
    assert calls.to(PROVISION) == []


@pytest.mark.asyncio
async def test_the_email_is_optional(bridge):
    """It is a typo check on the code, never the thing that identifies
    the account — so an installer who was given only a code can finish."""
    _instance, calls = bridge
    await provision(bridge, email=None)
    body = calls.to(PROVISION)[0][1]
    assert body["pairing_code"] == CODE
    assert body.get("email") is None


@pytest.mark.asyncio
async def test_the_token_is_never_logged(bridge, monkeypatch, capsys):
    """A bridge's bearer is fleet-wide until it is rotated."""
    lines: list[str] = []
    monkeypatch.setattr(server, "log", lines.append)
    await provision(bridge)
    assert not any(TOKEN in line for line in lines)


@pytest.mark.asyncio
async def test_a_backend_that_is_down_surfaces_as_a_failure(bridge):
    """The installer is standing in the house: a provision that did not
    happen must not read as one that did."""
    _instance, calls = bridge
    calls.raises = True
    with pytest.raises(Exception):
        await provision(bridge)
