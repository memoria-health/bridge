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
        audio_source_factory=fakes.FakeAudioSource,
        track_factory=lambda kind, _source: fakes.FakeTrack(kind),
        permission_factory=fakes.FakePermission,
    )


# ── the off switch ────────────────────────────────────────────────
async def test_a_disconnected_publisher_is_never_connected():
    p = build()
    await p.apply(pub(connected=False))
    assert fakes.FakeRoom.instances == []


async def test_flipping_to_disconnected_tears_the_room_down():
    p = build()
    await p.apply(pub())
    room = fakes.FakeRoom.instances[0]
    await p.apply(pub(connected=False))
    assert room.disconnected is True


async def test_a_publisher_absent_from_the_plan_is_torn_down():
    """A camera deleted in the dashboard vanishes from the roster; it
    must not keep publishing because nothing said 'disconnect'."""
    p = build()
    await p.apply(pub())
    room = fakes.FakeRoom.instances[0]
    await p.apply(None)
    assert room.disconnected is True


# ── the facilities audio kill ─────────────────────────────────────
async def test_audio_is_never_published_when_capture_is_disabled():
    p = build()
    await p.apply(pub(audio=False))
    kinds = [t.kind for t in fakes.FakeRoom.instances[0].local_participant.published]
    assert "audio" not in kinds


async def test_audio_is_published_when_capture_is_permitted():
    p = build()
    await p.apply(pub(audio=True))
    kinds = [t.kind for t in fakes.FakeRoom.instances[0].local_participant.published]
    assert "audio" in kinds


async def test_audio_is_removed_when_the_kill_arrives_mid_stream():
    """The gap: reconcile only connected and disconnected publishers, so
    a site turning audio off left the microphone open until something
    unrelated forced a reconnect."""
    p = build()
    await p.apply(pub(audio=True))
    participant = fakes.FakeRoom.instances[0].local_participant
    await p.apply(pub(audio=False))
    assert len(participant.unpublished) == 1
    assert isinstance(participant.unpublished[0], str), "must unpublish by SID"


async def test_the_camera_never_opens_an_audio_lane_nobody_wants():
    """Severed at the SOURCE. A muted track still means a decoder was
    fed the microphone."""
    source = fakes.FakeSource()
    p = build(source)
    await p.apply(pub(audio=False))
    assert source.audio_wanted == [False]


# ── bathroom and bedroom privacy ──────────────────────────────────
async def test_privacy_restricts_subscription_to_the_ai():
    p = build()
    await p.apply(pub(privacy=True))
    room = fakes.FakeRoom.instances[0]
    room.remote_participants = {
        "w": fakes.FakeRemote("detector_worker_abc"),
        "c": fakes.FakeRemote("caregiver_alice"),
    }
    await p.apply(pub(privacy=True))
    all_allowed, identities = room.local_participant.permission_calls[-1]
    assert all_allowed is False
    assert identities == ("detector_worker_abc",)


async def test_clearing_privacy_restores_everyone():
    p = build()
    await p.apply(pub(privacy=True))
    room = fakes.FakeRoom.instances[0]
    await p.apply(pub(privacy=False))
    assert room.local_participant.permission_calls[-1][0] is True


async def test_the_allow_list_is_real_participants_not_a_prefix_string():
    """The bug inherited from runtime.js: it allow-listed the literal
    string "detector_worker", which is not any participant's identity,
    so a private camera was invisible to the AI as well as to people."""
    p = build()
    await p.apply(pub(privacy=True))
    room = fakes.FakeRoom.instances[0]
    room.remote_participants = {
        "a": fakes.FakeRemote("detector_worker_7f3a"),
        "b": fakes.FakeRemote("caregiver_alice"),
    }
    await p.apply(pub(privacy=True))
    _, identities = room.local_participant.permission_calls[-1]
    assert "detector_worker_7f3a" in identities
    assert "detector_worker" not in identities


async def test_the_identity_service_is_allowed_on_a_private_camera():
    """It has its own prefix rather than a detector_worker_ name, because
    the backend's zombie reaper kicks detector_worker_* it did not spawn.
    Allow-listing only one prefix blinds it."""
    p = build()
    await p.apply(pub(privacy=True))
    room = fakes.FakeRoom.instances[0]
    room.remote_participants = {"i": fakes.FakeRemote("identity-service-01")}
    await p.apply(pub(privacy=True))
    _, identities = room.local_participant.permission_calls[-1]
    assert "identity-service-01" in identities


async def test_a_caregiver_is_never_allow_listed_on_a_private_camera():
    p = build()
    await p.apply(pub(privacy=True))
    room = fakes.FakeRoom.instances[0]
    room.remote_participants = {"c": fakes.FakeRemote("caregiver_bob")}
    await p.apply(pub(privacy=True))
    all_allowed, identities = room.local_participant.permission_calls[-1]
    assert all_allowed is False
    assert identities == ()


async def test_an_ai_worker_joining_later_is_picked_up():
    """Permissions are set before any subscriber exists, so a worker that
    joins afterwards is allow-listed only if we re-assert."""
    p = build()
    await p.apply(pub(privacy=True))
    room = fakes.FakeRoom.instances[0]
    assert room.local_participant.permission_calls[-1][1] == ()
    room.remote_participants = {"a": fakes.FakeRemote("detector_worker_1")}
    await p.apply(pub(privacy=True))
    assert room.local_participant.permission_calls[-1][1] == ("detector_worker_1",)


async def test_privacy_is_reasserted_on_every_reconcile():
    """Asserted once at connect is not enforcement. The gateway
    re-asserts every poll and this must match it, or a permission the
    SFU dropped or a flag flipped mid-stream is never re-applied."""
    p = build()
    await p.apply(pub(privacy=True))
    await p.apply(pub(privacy=True))
    await p.apply(pub(privacy=True))
    calls = fakes.FakeRoom.instances[0].local_participant.permission_calls
    assert len(calls) >= 3


async def test_privacy_turned_on_mid_stream_takes_effect_immediately():
    """This used to assert the room was NOT reconnected, which encoded
    the leak: permissions gate new subscriptions only, so leaving the
    publication in place left everyone already watching still watching."""
    p = build()
    await p.apply(pub(privacy=False))
    await p.apply(pub(privacy=True))
    room = fakes.FakeRoom.instances[-1]
    assert room.disconnected is False
    assert room.local_participant.permission_calls[-1][0] is False


async def test_a_failed_privacy_enforcement_tears_the_publisher_down():
    """Failing open here publishes a bathroom camera to every subscriber
    in the room. No picture is the correct outcome."""
    p = build()
    await p.apply(pub(privacy=True))
    room = fakes.FakeRoom.instances[0]
    room.local_participant.permissions_raise = True
    await p.apply(pub(privacy=True))
    assert room.disconnected is True


# ── fan-out ───────────────────────────────────────────────────────
async def test_one_camera_is_one_room_and_one_capture():
    """One physical camera is one row, one identity, one room. A camera
    covering several patients publishes a single stream into the
    primary's room; the others receive the RESULTS, never a second copy
    of the video. Provisioning used to mint a row per patient, which
    meant N identical streams at N times the ingest and model spend."""
    source = fakes.FakeSource()
    p = build(source)
    await p.apply(pub("cam_1", "patient_a"))
    assert source.starts == 1
    assert len(fakes.FakeRoom.instances) == 1
    published = fakes.FakeRoom.instances[0].local_participant.published
    assert [t.kind for t in published] == ["video"]


async def test_being_reassigned_to_another_room_moves_the_camera():
    """Coverage changed, so the primary changed, so the room changed.
    The old room must be left, not published into forever."""
    p = build()
    await p.apply(pub("cam_1", "patient_a"))
    first = fakes.FakeRoom.instances[0]
    await p.apply(pub("cam_2", "patient_b"))
    assert first.disconnected is True
    assert len(fakes.FakeRoom.instances) == 2
    assert p.connection.spec.room == "patient_b"


async def test_the_room_leaving_stops_the_camera():
    """Nothing wants this camera, so it must stop decoding rather than
    burn a core producing frames for nobody."""
    source = fakes.FakeSource()
    p = build(source)
    await p.apply(pub())
    await p.apply(None)
    assert source.stops == 1


# ── connection failures ───────────────────────────────────────────
async def test_a_failed_connect_does_not_leave_a_half_publisher():
    p = build()

    class Failing(fakes.FakeRoom):
        def __init__(self):
            super().__init__()
            self.connect_raises = True

    p.room_factory = Failing
    await p.apply(pub())
    assert p.connection is None


async def test_a_failed_connect_is_retried_on_the_next_reconcile():
    p = build()

    class Failing(fakes.FakeRoom):
        def __init__(self):
            super().__init__()
            self.connect_raises = True

    p.room_factory = Failing
    await p.apply(pub())
    p.room_factory = fakes.FakeRoom
    await p.apply(pub())
    assert p.connection is not None


async def test_the_camera_never_starts_while_livekit_is_unreachable():
    """Starting the capture BEFORE a room is connected meant ffmpeg and
    the browser page started and stopped on every 2s pass for as long as
    LiveKit was down — thrash, and a camera that looks alive in ps while
    publishing nothing."""
    source = fakes.FakeSource()
    p = build(source)

    class Failing(fakes.FakeRoom):
        def __init__(self):
            super().__init__()
            self.connect_raises = True

    p.room_factory = Failing
    for _ in range(3):
        await p.apply(pub())
    assert source.starts == 0
    assert source.stops == 0


# ── the audio lane, after the capture is already running ──────────
async def test_turning_audio_on_later_opens_the_lane():
    """want_audio was decided once, at capture start. A site that turned
    audio on afterwards got a published audio track backed by a lane
    that was never opened: silent, and indistinguishable from working
    audio."""
    source = fakes.FakeSource()
    p = build(source)
    await p.apply(pub(audio=False))
    assert source.audio_wanted == [False]
    await p.apply(pub(audio=True))
    assert source.audio_state[-1] is True


async def test_the_kill_cuts_the_lane_not_just_the_track():
    """Unpublishing stops it reaching LiveKit; cutting the lane stops the
    microphone being decoded at all. The backend calls this severed at
    the source, and a decoder still being fed is not severed."""
    source = fakes.FakeSource()
    p = build(source)
    await p.apply(pub(audio=True))
    await p.apply(pub(audio=False))
    assert source.audio_state[-1] is False


async def test_an_unchanged_audio_flag_does_not_churn_the_lane():
    """Re-applying every 2s must not restart a working decoder."""
    source = fakes.FakeSource()
    p = build(source)
    for _ in range(4):
        await p.apply(pub(audio=True))
    assert source.audio_state == []


async def test_reconnecting_does_not_start_a_second_capture():
    source = fakes.FakeSource()
    p = build(source)
    await p.apply(pub())
    await p.apply(pub())
    await p.apply(pub())
    assert source.starts == 1


# ── frames ────────────────────────────────────────────────────────
async def test_frames_reach_the_room():
    source = fakes.FakeSource([b"a", b"b", b"c"])
    p = build(source)
    await p.apply(pub())
    await p.pump()
    assert fakes.FakeVideoSource.created[0].captured == [b"a", b"b", b"c"]
    assert p.frames_published == 3


async def test_frames_are_dropped_while_no_room_is_connected():
    """Decoding can outlive a disconnect by a moment. Capturing into a
    torn-down source would be a use-after-free in the SDK."""
    source = fakes.FakeSource([b"a", b"b"])
    p = build(source)
    await p.pump()
    assert p.frames_published == 0


async def test_audio_is_only_captured_while_a_track_is_published():
    """The lane is cut at the source when the kill arrives, but a few
    already-decoded samples can still be in flight. They must not reach
    LiveKit after the track is gone.

    The earlier version of this test asserted `connection.audio_source is
    None`, which `apply()` alone establishes — deleting the pump call
    left it green. It proved nothing about the pump.
    """
    source = fakes.FakeSource()
    p = build(source)
    await p.apply(pub(audio=True))
    await p.pump_audio()
    captured = p.connection.audio_source.captured
    assert captured, "PCM must reach LiveKit while the track is published"

    before = len(captured)
    await p.apply(pub(audio=False))
    assert p.connection.audio_source is None
    await p.pump_audio()
    assert len(captured) == before, "no PCM may reach LiveKit after the kill"


# ── what actually took effect ─────────────────────────────────────
async def test_privacy_is_set_before_the_track_is_published():
    """Applying it after publishing leaves a window where a subscriber
    can auto-subscribe to a bathroom camera. The permission is
    participant-level, so it costs nothing to set it first."""
    p = build()
    await p.apply(pub(privacy=True))
    participant = fakes.FakeRoom.instances[0].local_participant
    assert participant.permission_calls, "privacy must be applied at connect"
    assert participant.order.index("permissions") < participant.order.index("publish")


async def test_applied_reports_effect_not_intent():
    """The contract calls this the difference between "the backend sent
    disconnect" and "the camera stopped publishing". Reading it off the
    plan would make it a restatement of the plan."""
    p = build()
    await p.apply(pub(privacy=True, audio=True))
    assert p.applied["privacy_enabled"] is True
    assert p.applied["audio_capture_enabled"] is True


async def test_applied_says_audio_is_off_when_no_track_exists():
    p = build()
    await p.apply(pub(audio=False))
    assert p.applied["audio_capture_enabled"] is False


async def test_applied_does_not_claim_success_when_privacy_failed():
    """The failure mode this guards: an operator checks whether a
    bathroom reclassify took, and sees confirmation of something that
    never happened."""
    p = build()
    await p.apply(pub(privacy=True))
    fakes.FakeRoom.instances[0].local_participant.permissions_raise = True
    await p.apply(pub(privacy=True))
    assert p.applied["desired_state"] == "disconnected"


async def test_the_kill_unpublishes_before_cutting_the_lane():
    """Cutting first leaves already-decoded PCM able to reach LiveKit
    through the pump, because the track is still published."""
    source = fakes.FakeSource()
    p = build(source)
    await p.apply(pub(audio=True))
    participant = fakes.FakeRoom.instances[0].local_participant
    await p.apply(pub(audio=False))
    assert len(participant.unpublished) == 1
    assert isinstance(participant.unpublished[0], str), "must unpublish by SID"
    assert source.audio_state[-1] is False
    assert participant.order.index("unpublish") < len(participant.order)


async def test_publishing_a_frame_is_what_proves_the_chain_works():
    """Frames arriving at the browser prove the camera is alive. Only
    this proves they reached LiveKit."""
    p = build(fakes.FakeSource([b"a"]))
    await p.apply(pub())
    assert p.last_published_at == 0.0
    await p.pump()
    assert p.last_published_at > 0.0


async def test_teardown_survives_a_track_without_stop():
    """The Python SDK's LocalVideoTrack has no stop(); that is a browser
    idiom. Calling it unconditionally raised inside teardown, after the
    room disconnect had already happened — leaving the publisher
    half-torn-down with its connection already cleared."""
    class NoStop:
        def __init__(self, kind):
            self.kind = kind

    p = CameraPublisher(
        camera_id="warner_abc", source=fakes.FakeSource(),
        livekit_url="wss://test", token_for=lambda i, r: "t",
        room_factory=fakes.FakeRoom,
        video_source_factory=fakes.FakeVideoSource,
        audio_source_factory=fakes.FakeAudioSource,
        track_factory=lambda kind, _s: NoStop(kind),
        permission_factory=fakes.FakePermission,
    )
    await p.apply(pub())
    await p.close()
    assert p.connection is None


async def test_privacy_never_interrupts_the_stream():
    """The AI must not go blind to enforce a rule about human viewing.

    Republishing on the transition would drop every existing subscriber,
    which is the point for a caregiver — but the detector and the
    identity service are subscribers too, and the product promise is that
    a private room keeps producing alerts while no human sees the video.
    Interrupting the AI to enforce something the app already enforces in
    its live view is the wrong trade.
    """
    p = build()
    await p.apply(pub(privacy=False))
    room = fakes.FakeRoom.instances[0]
    await p.apply(pub(privacy=True))
    await p.apply(pub(privacy=False))
    assert room.disconnected is False
    assert len(fakes.FakeRoom.instances) == 1
