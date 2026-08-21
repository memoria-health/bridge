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


class FakeParticipant:
    def __init__(self):
        self.published: list[FakeTrack] = []
        self.unpublished: list[FakeTrack] = []
        # Every call, so a test can assert re-assertion rather than
        # merely that it happened once at connect time.
        self.permission_calls: list[tuple[bool, tuple[str, ...]]] = []
        self.permissions_raise = False

    async def publish_track(self, track, options=None):
        self.published.append(track)
        return track

    async def unpublish_track(self, track):
        self.unpublished.append(track)

    async def set_track_subscription_permissions(self, all_allowed, perms=None):
        if self.permissions_raise:
            raise RuntimeError("livekit refused the permission update")
        identities = tuple(p["participantIdentity"] for p in (perms or []))
        self.permission_calls.append((all_allowed, identities))


class FakeRoom:
    instances: list["FakeRoom"] = []

    def __init__(self):
        self.local_participant = FakeParticipant()
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
        self._frames = list(frames or [b"frame-1", b"frame-2"])
        self._audio = [b"pcm-1"]

    async def start(self, want_audio: bool = True) -> None:
        self.audio_wanted.append(want_audio)
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1

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


def reset() -> None:
    FakeRoom.instances.clear()
    FakeVideoSource.created.clear()
