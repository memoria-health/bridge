"""Publishing one camera into the rooms the backend currently wants.

The three bugs this replaces, all of which shipped: a disconnect that
did nothing, a privacy flag applied once at connect and never again, and
an audio kill that was received on every poll and ignored.
"""
from __future__ import annotations

import pytest

from core.publisher import CameraPublisher
from test import fakes
from vendors.base import Publisher


@pytest.fixture(autouse=True)
def _clean():
    fakes.reset()
    yield
    fakes.reset()


def pub(identity="cam_1", room="patient_a", connected=True,
        audio=False, privacy=False) -> Publisher:
    return Publisher(identity=identity, room=room, connected=connected,
                     audio_capture_enabled=audio, privacy_enabled=privacy)


def build(source=None) -> CameraPublisher:
    return CameraPublisher(
        camera_id="warner_abc",
        source=source or fakes.FakeSource(),
        livekit_url="wss://test",
        token_for=lambda identity, room: f"token-{identity}",
        room_factory=fakes.FakeRoom,
        video_source_factory=fakes.FakeVideoSource,
        audio_source_factory=lambda: object(),
        track_factory=lambda kind, _source: fakes.FakeTrack(kind),
    )


# ── the off switch ────────────────────────────────────────────────
async def test_a_disconnected_publisher_is_never_connected():
    p = build()
    await p.apply([pub(connected=False)])
    assert fakes.FakeRoom.instances == []


async def test_flipping_to_disconnected_tears_the_room_down():
    p = build()
    await p.apply([pub()])
    room = fakes.FakeRoom.instances[0]
    await p.apply([pub(connected=False)])
    assert room.disconnected is True


async def test_a_publisher_absent_from_the_plan_is_torn_down():
    """A camera deleted in the dashboard vanishes from the roster; it
    must not keep publishing because nothing said 'disconnect'."""
    p = build()
    await p.apply([pub()])
    room = fakes.FakeRoom.instances[0]
    await p.apply([])
    assert room.disconnected is True


# ── the facilities audio kill ─────────────────────────────────────
async def test_audio_is_never_published_when_capture_is_disabled():
    p = build()
    await p.apply([pub(audio=False)])
    kinds = [t.kind for t in fakes.FakeRoom.instances[0].local_participant.published]
    assert "audio" not in kinds


async def test_audio_is_published_when_capture_is_permitted():
    p = build()
    await p.apply([pub(audio=True)])
    kinds = [t.kind for t in fakes.FakeRoom.instances[0].local_participant.published]
    assert "audio" in kinds


async def test_audio_is_removed_when_the_kill_arrives_mid_stream():
    """The gap: reconcile only connected and disconnected publishers, so
    a site turning audio off left the microphone open until something
    unrelated forced a reconnect."""
    p = build()
    await p.apply([pub(audio=True)])
    participant = fakes.FakeRoom.instances[0].local_participant
    await p.apply([pub(audio=False)])
    assert [t.kind for t in participant.unpublished] == ["audio"]


async def test_the_camera_never_opens_an_audio_lane_nobody_wants():
    """Severed at the SOURCE. A muted track still means a decoder was
    fed the microphone."""
    source = fakes.FakeSource()
    p = build(source)
    await p.apply([pub(audio=False)])
    assert source.audio_wanted == [False]


# ── bathroom and bedroom privacy ──────────────────────────────────
async def test_privacy_restricts_subscription_to_the_ai():
    p = build()
    await p.apply([pub(privacy=True)])
    room = fakes.FakeRoom.instances[0]
    room.remote_participants = {
        "w": fakes.FakeRemote("detector_worker_abc"),
        "c": fakes.FakeRemote("caregiver_alice"),
    }
    await p.apply([pub(privacy=True)])
    all_allowed, identities = room.local_participant.permission_calls[-1]
    assert all_allowed is False
    assert identities == ("detector_worker_abc",)


async def test_clearing_privacy_restores_everyone():
    p = build()
    await p.apply([pub(privacy=True)])
    room = fakes.FakeRoom.instances[0]
    await p.apply([pub(privacy=False)])
    assert room.local_participant.permission_calls[-1][0] is True


async def test_the_allow_list_is_real_participants_not_a_prefix_string():
    """The bug inherited from runtime.js: it allow-listed the literal
    string "detector_worker", which is not any participant's identity,
    so a private camera was invisible to the AI as well as to people."""
    p = build()
    await p.apply([pub(privacy=True)])
    room = fakes.FakeRoom.instances[0]
    room.remote_participants = {
        "a": fakes.FakeRemote("detector_worker_7f3a"),
        "b": fakes.FakeRemote("caregiver_alice"),
    }
    await p.apply([pub(privacy=True)])
    _, identities = room.local_participant.permission_calls[-1]
    assert "detector_worker_7f3a" in identities
    assert "detector_worker" not in identities


async def test_the_identity_service_is_allowed_on_a_private_camera():
    """It has its own prefix rather than a detector_worker_ name, because
    the backend's zombie reaper kicks detector_worker_* it did not spawn.
    Allow-listing only one prefix blinds it."""
    p = build()
    await p.apply([pub(privacy=True)])
    room = fakes.FakeRoom.instances[0]
    room.remote_participants = {"i": fakes.FakeRemote("identity-service-01")}
    await p.apply([pub(privacy=True)])
    _, identities = room.local_participant.permission_calls[-1]
    assert "identity-service-01" in identities


async def test_a_caregiver_is_never_allow_listed_on_a_private_camera():
    p = build()
    await p.apply([pub(privacy=True)])
    room = fakes.FakeRoom.instances[0]
    room.remote_participants = {"c": fakes.FakeRemote("caregiver_bob")}
    await p.apply([pub(privacy=True)])
    all_allowed, identities = room.local_participant.permission_calls[-1]
    assert all_allowed is False
    assert identities == ()


async def test_an_ai_worker_joining_later_is_picked_up():
    """Permissions are set before any subscriber exists, so a worker that
    joins afterwards is allow-listed only if we re-assert."""
    p = build()
    await p.apply([pub(privacy=True)])
    room = fakes.FakeRoom.instances[0]
    assert room.local_participant.permission_calls[-1][1] == ()
    room.remote_participants = {"a": fakes.FakeRemote("detector_worker_1")}
    await p.apply([pub(privacy=True)])
    assert room.local_participant.permission_calls[-1][1] == ("detector_worker_1",)


async def test_privacy_is_reasserted_on_every_reconcile():
    """Asserted once at connect is not enforcement. The gateway
    re-asserts every poll and this must match it, or a permission the
    SFU dropped or a flag flipped mid-stream is never re-applied."""
    p = build()
    await p.apply([pub(privacy=True)])
    await p.apply([pub(privacy=True)])
    await p.apply([pub(privacy=True)])
    calls = fakes.FakeRoom.instances[0].local_participant.permission_calls
    assert len(calls) >= 3


async def test_privacy_turned_on_mid_stream_applies_without_reconnecting():
    p = build()
    await p.apply([pub(privacy=False)])
    room = fakes.FakeRoom.instances[0]
    await p.apply([pub(privacy=True)])
    assert room.disconnected is False
    assert room.local_participant.permission_calls[-1][0] is False


async def test_a_failed_privacy_enforcement_tears_the_publisher_down():
    """Failing open here publishes a bathroom camera to every subscriber
    in the room. No picture is the correct outcome."""
    p = build()
    await p.apply([pub(privacy=True)])
    room = fakes.FakeRoom.instances[0]
    room.local_participant.permissions_raise = True
    await p.apply([pub(privacy=True)])
    assert room.disconnected is True


# ── fan-out ───────────────────────────────────────────────────────
async def test_one_camera_in_two_rooms_is_one_capture():
    """A shared camera in a two-patient home. Two captures would be
    twice the decode, twice the LiveKit bill, and two chances to drift."""
    source = fakes.FakeSource()
    p = build(source)
    await p.apply([pub("cam_1", "patient_a"), pub("cam_2", "patient_b")])
    assert source.starts == 1
    assert len(fakes.FakeRoom.instances) == 2


async def test_each_room_gets_its_own_video_track():
    p = build()
    await p.apply([pub("cam_1", "patient_a"), pub("cam_2", "patient_b")])
    for room in fakes.FakeRoom.instances:
        assert [t.kind for t in room.local_participant.published] == ["video"]


async def test_dropping_one_room_leaves_the_other_publishing():
    p = build()
    await p.apply([pub("cam_1", "patient_a"), pub("cam_2", "patient_b")])
    first, second = fakes.FakeRoom.instances
    await p.apply([pub("cam_1", "patient_a")])
    assert first.disconnected is False
    assert second.disconnected is True


async def test_the_last_room_leaving_stops_the_camera():
    """Nothing wants this camera, so it must stop decoding rather than
    burn a core producing frames for nobody."""
    source = fakes.FakeSource()
    p = build(source)
    await p.apply([pub()])
    await p.apply([])
    assert source.stops == 1


# ── connection failures ───────────────────────────────────────────
async def test_a_failed_connect_does_not_leave_a_half_publisher():
    p = build()

    class Failing(fakes.FakeRoom):
        def __init__(self):
            super().__init__()
            self.connect_raises = True

    p.room_factory = Failing
    await p.apply([pub()])
    assert p.rooms == {}


async def test_a_failed_connect_is_retried_on_the_next_reconcile():
    p = build()

    class Failing(fakes.FakeRoom):
        def __init__(self):
            super().__init__()
            self.connect_raises = True

    p.room_factory = Failing
    await p.apply([pub()])
    p.room_factory = fakes.FakeRoom
    await p.apply([pub()])
    assert "cam_1" in p.rooms


async def test_reconnecting_does_not_start_a_second_capture():
    source = fakes.FakeSource()
    p = build(source)
    await p.apply([pub()])
    await p.apply([pub()])
    await p.apply([pub()])
    assert source.starts == 1
