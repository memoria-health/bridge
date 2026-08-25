"""The vendor reconcile loop, which had no tests at all.

Every finding in this file's subject area was found by review rather than
by a failing test, which is the argument for the file existing.
"""
from __future__ import annotations

import pytest

from vendors.base import EntityHealth, Publisher
from vendors.warner import WarnerVendor


def pub(identity="cam_1", room="patient_a", connected=True,
        audio=False, privacy=False) -> Publisher:
    return Publisher(identity=identity, room=room, connected=connected,
                     audio_capture_enabled=audio, privacy_enabled=privacy)


class FakePublisher:
    def __init__(self, camera_id="c", explodes=False):
        self.camera_id = camera_id
        self.applied: dict = {}
        self.connection = object()
        self.last_published_at = 5.0
        self.calls: list = []
        self.explodes = explodes
        self.closed = False

    async def apply(self, spec):
        self.calls.append(spec)
        if self.explodes:
            raise RuntimeError("livekit blew up for this camera")

    async def close(self):
        self.closed = True


@pytest.fixture
def vendor(monkeypatch):
    v = WarnerVendor()
    monkeypatch.setattr(v.chromium, "apply", _noop)
    return v


async def _noop(_plan):
    return None


def install(v: WarnerVendor, camera_id: str, publisher) -> None:
    v.publishers[camera_id] = publisher
    v.chromium.status[camera_id] = EntityHealth(last_frame_at=5.0)


# ── one camera's failure must not silence the others ──────────────
async def test_one_camera_blowing_up_does_not_skip_the_rest(vendor):
    """The failure this guards is a privacy fail-open. An unguarded raise
    in camera A's reconcile unwound to the supervisor's log-and-sleep,
    so every camera LATER in inventory order was skipped that pass — and
    a camera reclassified as a bathroom in the app never had its
    subscription permissions applied at all, indefinitely."""
    bad, good = FakePublisher("a", explodes=True), FakePublisher("b")
    install(vendor, "a", bad)
    install(vendor, "b", good)

    await vendor.apply({"a": pub("cam_a"), "b": pub("cam_b", privacy=True)})

    assert good.calls, "a camera after the failing one must still reconcile"


async def test_the_failing_camera_is_retried_next_pass(vendor):
    bad = FakePublisher("a", explodes=True)
    install(vendor, "a", bad)
    await vendor.apply({"a": pub("cam_a")})
    await vendor.apply({"a": pub("cam_a")})
    assert len(bad.calls) == 2


# ── health reflects the whole chain, not just the browser ─────────
async def test_health_reports_what_the_publisher_actually_applied(vendor):
    """`applied` used to be copied off the plan by the page supervisor,
    before anything had been attempted. An operator checking whether a
    bathroom reclassify took saw confirmation of a thing that had not
    happened."""
    publisher = FakePublisher("a")
    publisher.applied = {"desired_state": "connected", "privacy_enabled": True}
    install(vendor, "a", publisher)
    await vendor.apply({"a": pub("cam_a", privacy=True)})
    assert vendor.health()["a"].applied["privacy_enabled"] is True


async def test_a_camera_that_is_not_publishing_is_not_healthy(vendor):
    publisher = FakePublisher("a")
    publisher.connection = None
    publisher.last_published_at = 0.0
    install(vendor, "a", publisher)
    await vendor.apply({"a": pub("cam_a")})
    health = vendor.health()["a"]
    assert health.publishing is False
    assert health.healthy(now=6.0, stall_seconds=20) is False


async def test_a_publishing_camera_is_healthy(vendor):
    install(vendor, "a", FakePublisher("a"))
    await vendor.apply({"a": pub("cam_a")})
    assert vendor.health()["a"].healthy(now=6.0, stall_seconds=20) is True


# ── retire ────────────────────────────────────────────────────────
async def test_a_camera_dropped_from_the_plan_is_closed(vendor):
    publisher = FakePublisher("a")
    install(vendor, "a", publisher)
    await vendor.apply({"a": pub("cam_a")})
    await vendor.apply({})
    assert publisher.closed is True
    assert "a" not in vendor.publishers
