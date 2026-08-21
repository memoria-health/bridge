"""Fake LiveKit and a fake camera, so the publish rules can be tested
without a network, a GPU, or a physical camera."""
from __future__ import annotations

import asyncio
from typing import Any


class FakeTrack:
    def __init__(self, kind: str):
        self.kind = kind
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakePublication:
    """What publish_track returns. `unpublish_track` takes this SID."""

    _next = 0

    def __init__(self, track):
        FakePublication._next += 1
        self.sid = f"TR_{FakePublication._next}"
        self.track = track


class FakeParticipant:
    def __init__(self):
        self.published: list[FakeTrack] = []
        self.unpublished: list[FakeTrack] = []
        # Every call, so a test can assert re-assertion rather than
        # merely that it happened once at connect time.
        self.permission_calls: list[tuple[bool, tuple[str, ...]]] = []
        self.permissions_raise = False
        # Call ORDER, so a test can prove privacy lands before the track
        # rather than merely that both happened.
        self.order: list[str] = []

    async def publish_track(self, track, options=None):
        self.order.append("publish")
        self.published.append(track)
        return FakePublication(track)

    async def unpublish_track(self, track_sid):
        """Takes a SID STRING, matching the real SDK. The earlier fake
        accepted a track object, so it recorded a call that raises
        against LiveKit — every audio kill, every time."""
        if not isinstance(track_sid, str):
            raise TypeError(
                f"unpublish_track wants a track SID, got {type(track_sid).__name__}")
        self.order.append("unpublish")
        self.unpublished.append(track_sid)

    async def set_track_subscription_permissions(
        self, *, allow_all_participants, participant_permissions=None,
    ):
        """KEYWORD-ONLY, matching the real SDK.

        The earlier fake accepted positional arguments and dicts, so it
        happily recorded a call that raises TypeError against LiveKit —
        every private camera, every time. A fake that is more permissive
        than the thing it stands in for cannot catch that.
        """
        if self.permissions_raise:
            raise RuntimeError("livekit refused the permission update")
        self.order.append("permissions")
        identities = tuple(p.participant_identity
                           for p in (participant_permissions or []))
        self.permission_calls.append((allow_all_participants, identities))


class FakePermission:
    """Stands in for rtc.ParticipantTrackPermission."""

    def __init__(self, participant_identity: str, allow_all: bool = True):
        self.participant_identity = participant_identity
        self.allow_all = allow_all


class FakeRemote:
    def __init__(self, identity: str):
        self.identity = identity


class FakeRoom:
    instances: list["FakeRoom"] = []

    def __init__(self):
        self.local_participant = FakeParticipant()
        # Who else is in the room. The privacy allow-list is built from
        # these, so a subscriber that is not here cannot be allowed.
        self.remote_participants: dict[str, FakeRemote] = {}
        self.connected_to: tuple[str, str] | None = None
        self.disconnected = False
        self.connect_raises = False
        FakeRoom.instances.append(self)

    async def connect(self, url, token, options=None):
        if self.connect_raises:
            raise RuntimeError("livekit unreachable")
        self.connected_to = (url, token)

    async def disconnect(self):
        self.disconnected = True


class FakeSource:
    """One physical camera. Counts starts so a test can prove that N
    rooms share ONE capture rather than opening N."""

    def __init__(self, frames: list[bytes] | None = None):
        self.width, self.height, self.fps = 640, 360, 15
        self.starts = 0
        self.stops = 0
        self.audio_wanted: list[bool] = []
        # Every later change to the lane, so a test can prove audio can
        # be opened or cut AFTER the capture is already running.
        self.audio_state: list[bool] = []
        self._frames = list(frames or [b"frame-1", b"frame-2"])
        self._audio = [b"pcm-1"]

    async def start(self, want_audio: bool = True) -> None:
        self.audio_wanted.append(want_audio)
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1

    async def set_audio(self, enabled: bool) -> None:
        self.audio_state.append(enabled)

    async def video_frames(self):
        for frame in self._frames:
            yield frame
            await asyncio.sleep(0)

    async def audio_frames(self):
        for chunk in self._audio:
            yield chunk
            await asyncio.sleep(0)


class FakeVideoSource:
    created: list["FakeVideoSource"] = []

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.captured: list[Any] = []
        FakeVideoSource.created.append(self)

    def capture_frame(self, frame) -> None:
        self.captured.append(frame)


class FakeAudioSource:
    def __init__(self):
        self.captured: list[bytes] = []

    async def capture_frame(self, pcm) -> None:
        self.captured.append(pcm)


def reset() -> None:
    FakeRoom.instances.clear()
    FakeVideoSource.created.clear()
