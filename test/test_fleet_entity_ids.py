"""A poll reports the BACKEND's camera uuid, not our vendor key.

Contract rule 3: an entity id is `<camera uuid>`. We sent our own key
("warner_8d2506f65170d9f3"), which the backend feeds into a `uuid[]`
bind — so every poll 500'd on `invalid UUID ... got 23`. One malformed
id fails the whole statement, so NO camera on this bridge ever had its
heartbeat stamped: the camera published perfectly and read as never
seen, which is precisely the state the backend's staleness sweep exists
to catch.

Found on the first real Warner install (2026-08-23), by noticing
`last_heartbeat_at` was NULL on a camera that was plainly streaming.

`livekit_identity` is the join. It is the one field both sides already
agree on: ours from provisioning, theirs from the roster.
"""
from __future__ import annotations

import pytest

from core import server, store
from core.server import Bridge
from vendors.base import EntityHealth
from vendors.warner import WarnerVendor

VENDOR_KEY = "warner_8d2506f65170d9f3"
BACKEND_UUID = "c51733e6-0cc7-4f0d-a785-51747905eb0b"
IDENTITY = "cam_c51733e6"


def _health() -> EntityHealth:
    return EntityHealth(
        state="publishing", last_frame_at=1.0, frame_count=10,
        publishing=True, last_publish_at=1.0,
        applied={"desired_state": "connected", "room": "account_x",
                 "privacy_enabled": False, "audio_capture_enabled": True},
    )


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "INVENTORY_PATH", tmp_path / "inventory.json")
    store.write_inventory({"version": 1, "cameras": [
        {"camera_id": VENDOR_KEY, "name": "TV Room",
         "livekit_identity": IDENTITY, "device_key": "p:d:s"},
    ]})
    instance = Bridge(
        {"backend_url": "https://backend", "gateway_token": "t",
         "instance_id": "bridge-test", "livekit_url": "wss://lk",
         "poll_seconds": 3, "stream_mode": "continuous"},
        WarnerVendor())
    instance.roster.items = [
        {"camera_id": BACKEND_UUID, "livekit_identity": IDENTITY,
         "livekit_room": "account_x", "desired_state": "connected"},
    ]
    return instance


def test_the_report_carries_the_backend_uuid(bridge, monkeypatch):
    """THE regression. A vendor key here loses every heartbeat."""
    monkeypatch.setattr(bridge.vendor, "health", lambda: {VENDOR_KEY: _health()})

    ids = [e["id"] for e in bridge.fleet_report()["entities"]]

    assert ids == [BACKEND_UUID], (
        "reported our own key; the backend cannot cast it to a uuid and "
        "the whole poll fails, taking every sibling's heartbeat with it"
    )


def test_an_unmappable_camera_is_dropped_not_invented(bridge, monkeypatch):
    """A camera the backend has forgotten must read as ABSENT. Sending it
    under a made-up id is what breaks the poll for everyone else."""
    monkeypatch.setattr(
        bridge.vendor, "health",
        lambda: {VENDOR_KEY: _health(), "warner_unknown": _health()})

    ids = [e["id"] for e in bridge.fleet_report()["entities"]]

    assert ids == [BACKEND_UUID]


def test_an_empty_roster_reports_no_entities(bridge, monkeypatch):
    """Before the first successful poll there is nothing to map against,
    and guessing would put un-castable ids on the wire."""
    bridge.roster.items = []
    monkeypatch.setattr(bridge.vendor, "health", lambda: {VENDOR_KEY: _health()})

    assert bridge.fleet_report()["entities"] == []
