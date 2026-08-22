"""What this service tells the backend it IS.

A hosted bridge serves many customer accounts at once, and the backend
scopes a service's camera roster by gateway alone — the multi-tenant
behaviour — only when its `public.gateways` row says so. It sets that
from the `service_kind` sent at enrolment, and the bridge sent none. So
its row was created as a single-tenant BOX in one home: provisioning a
camera for customer B rebound the whole row to B, and every camera of
customer A dropped off the roster and stopped publishing, silently.
"""
from __future__ import annotations

import pytest

from core import server, store
from core.server import Bridge
from vendors.warner import WarnerVendor

ENROLL = "https://backend/v1/gateway/enroll"
PROVISION = "https://backend/v1/gateway/provision-camera"
TOKEN = "T" * 43


class Calls(list):
    """Every backend POST, so a test can assert on the body AND on which
    endpoint received it."""

    raises = False
    fails = ""

    def __call__(self, url, body, headers=None):
        if self.raises or (self.fails and url == self.fails):
            raise RuntimeError("backend unreachable")
        self.append((url, body, headers or {}))
        return {"ok": True, "already_enrolled": False,
                "cameras": [{"livekit_identity": "cam_1"}]}

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


async def provision(bridge, email="caregiver@example.com", device="p:d1:s"):
    instance, _ = bridge
    return await instance.provision(FakeRequest(
        {"email": email, "name": "Den", "device_key": device}))


# ── the common path: say what we are while claiming a camera ──────
async def test_provisioning_declares_the_service_kind(bridge):
    """Without this the backend has no way to tell a hosted bridge from a
    Pi in someone's living room, and defaults to the living room."""
    _, calls = bridge
    await provision(bridge)
    assert calls.to(PROVISION)[0][1]["service_kind"] == "camera-bridge"


async def test_provisioning_also_enrols_explicitly(bridge):
    """provision-camera creating the row as a side effect is what let a
    row exist that had never been told what it belonged to."""
    _, calls = bridge
    await provision(bridge)
    enrolments = calls.to(ENROLL)
    assert len(enrolments) == 1
    assert enrolments[0][1] == {"email": "caregiver@example.com",
                                "service_kind": "camera-bridge"}
    assert enrolments[0][2]["Authorization"] == f"Bearer {TOKEN}"


# ── the ordering problem: a fresh bridge has no email ─────────────
async def test_the_provisioning_email_is_remembered(bridge):
    """Enrolment needs a caregiver email and a freshly installed bridge
    has none until a human provisions. Remembering it is the only way a
    reboot can re-declare what we are without one."""
    await provision(bridge)
    assert store.account_email() == "caregiver@example.com"


async def test_a_bridge_that_owns_cameras_re_enrols_on_boot(bridge):
    """The self-heal. A row created as a single-tenant box before this
    fix stays wrong forever otherwise, and its owner never sees why the
    cameras stopped."""
    instance, calls = bridge
    await provision(bridge)
    calls.clear()

    await instance.enroll_stored()

    assert calls.to(ENROLL), "a bridge with cameras must re-declare itself"
    assert not calls.to(PROVISION), "boot must not mint new camera rows"


async def test_a_bridge_with_no_cameras_stays_quiet(bridge):
    """Nothing to repair, and no email that could be replayed against the
    wrong account."""
    instance, calls = bridge
    await instance.enroll_stored()
    assert calls == []


async def test_a_backend_that_is_down_at_boot_does_not_stop_the_bridge(bridge):
    instance, calls = bridge
    await provision(bridge)
    calls.raises = True
    await instance.enroll_stored()      # must not raise


async def test_a_failed_enrolment_does_not_fail_the_provisioning(bridge):
    """The camera row already exists at that point. Returning an error
    would tell the operator to provision again, which mints a second one."""
    _, calls = bridge
    calls.fails = ENROLL
    response = await provision(bridge)
    assert response.status == 200
    assert calls.to(PROVISION), "the camera was still claimed"


# ── never log the credential ──────────────────────────────────────
async def test_enrolment_never_logs_the_token(bridge, monkeypatch):
    instance, _ = bridge
    lines: list[str] = []
    monkeypatch.setattr(server, "log", lines.append)
    await instance.enroll("caregiver@example.com")
    printed = "\n".join(lines)
    assert printed, "enrolment is worth one line in the log"
    assert TOKEN not in printed
