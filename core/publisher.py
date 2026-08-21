#!/usr/bin/env python3
"""One camera into the one room it belongs to.

Publishes from Python through a LiveKit `VideoSource` as an ordinary
`cam_*` participant, the way `nemara-gateway/publisher.py` already does
in production: no Ingress, no per-minute ingest bill, and no new
participant identity for the webhooks and `room_reconciler` to trip on.

The rule that shapes this file: **every flag is re-applied on every
reconcile.** The browser version applied privacy and the audio kill once,
at connect time, and then only ever connected or disconnected
publishers — so a site turning audio off, or a room being marked as a
bathroom, changed a database row and nothing else until something
unrelated forced a reconnect. Enforcement that happens once is not
enforcement.

LiveKit is injected rather than imported so the rules above can be
tested without a network.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from core.util import log
from vendors.base import Publisher

# Only analysis workers may subscribe to a private camera. Two prefixes,
# because the identity service deliberately does NOT use a
# detector_worker_ name — the backend's startup zombie reaper kicks
# detector_worker_* participants it did not spawn. Allow-listing one
# prefix blinds the other.
AI_SUBSCRIBER_PREFIXES = ("detector_worker_", "identity-service")


class _Connection:
    """One LiveKit room this camera appears in."""

    def __init__(self, spec: Publisher, room: Any, video_source: Any,
                 video_track: Any):
        self.spec = spec
        self.room = room
        self.video_source = video_source
        self.video_track = video_track
        self.audio_track: Any = None
        self.privacy_applied: bool | None = None


class CameraPublisher:
    def __init__(
        self,
        camera_id: str,
        source: Any,
        livekit_url: str,
        token_for: Callable[[str, str], str],
        room_factory: Callable[[], Any],
        video_source_factory: Callable[[int, int], Any],
        audio_source_factory: Callable[[], Any] | None = None,
        track_factory: Callable[[str, Any], Any] | None = None,
    ):
        self.camera_id = camera_id
        self.source = source
        self.livekit_url = livekit_url
        self.token_for = token_for
        self.room_factory = room_factory
        self.video_source_factory = video_source_factory
        self.audio_source_factory = audio_source_factory
        self.track_factory = track_factory or _default_track
        self.connection: _Connection | None = None
        self.frames_published = 0
        self._capturing = False

    # ── reconcile ─────────────────────────────────────────────────
    async def apply(self, publisher: Publisher | None) -> None:
        """Bring reality in line with what the backend wants. Idempotent.

        ONE room, because one physical camera is one row, one identity,
        one room. A camera covering several patients still publishes a
        single stream into the primary's room; the others receive the
        results. Fanning one capture into N rooms was the pre-2026-08-19
        model and cost N times the ingest for identical footage.
        """
        wanted = publisher if (publisher and publisher.connected) else None

        if self.connection and (
                wanted is None or wanted.identity != self.connection.spec.identity):
            await self._disconnect(
                "no longer wanted" if wanted is None else "reassigned")

        if wanted is None:
            if self._capturing:
                await self.source.stop()
                self._capturing = False
            return

        if not self._capturing:
            await self.source.start(want_audio=wanted.audio_capture_enabled)
            self._capturing = True

        if self.connection is None:
            await self._connect(wanted)

        # EVERY pass, including when the room was already connected
        # before this call. Enforcement that happens once is not
        # enforcement.
        if self.connection is not None:
            self.connection.spec = wanted
            await self._enforce(self.connection)

        if self.connection is None and self._capturing:
            await self.source.stop()
            self._capturing = False

    async def _connect(self, spec: Publisher) -> None:
        room = self.room_factory()
        try:
            await room.connect(self.livekit_url,
                               self.token_for(spec.identity, spec.room))
            video_source = self.video_source_factory(
                self.source.width, self.source.height)
            track = self.track_factory("video", video_source)
            await room.local_participant.publish_track(track)
        except Exception as exc:  # noqa: BLE001 — retried on the next pass
            log(f"[{self.camera_id}] connect {spec.identity} failed: {exc}")
            try:
                await room.disconnect()
            except Exception:  # noqa: BLE001 — never completed
                pass
            return
        self.connection = _Connection(spec, room, video_source, track)
        log(f"[{self.camera_id}] publishing {spec.identity} -> {spec.room}")

    async def _disconnect(self, why: str) -> None:
        connection, self.connection = self.connection, None
        if not connection:
            return
        identity = connection.spec.identity
        try:
            await connection.room.disconnect()
        except Exception:  # noqa: BLE001 — already gone
            pass
        for track in (connection.video_track, connection.audio_track):
            if track is not None:
                track.stop()
        log(f"[{self.camera_id}] stopped {identity}: {why}")

    # ── the two safety flags ──────────────────────────────────────
    async def _enforce(self, connection: _Connection) -> None:
        try:
            await self._enforce_privacy(connection)
        except Exception as exc:  # noqa: BLE001
            # Failing OPEN would publish a bathroom camera to every
            # subscriber in the room. No picture is the right outcome.
            log(f"[{self.camera_id}] privacy enforcement failed for "
                f"{connection.spec.identity}: {exc}; tearing down")
            await self._disconnect("privacy unenforceable")
            return
        await self._enforce_audio(connection)

    async def _enforce_privacy(self, connection: _Connection) -> None:
        """Allow-list the AI participants ACTUALLY in the room.

        LiveKit matches a permission entry by exact participant identity,
        so a prefix string allow-lists nobody. `runtime.js` passed the
        literal "detector_worker" and therefore made a private camera
        invisible to the AI as well as to people. Rebuilt every pass,
        which is also how a worker that joins later gets allowed.
        """
        participant = connection.room.local_participant
        if connection.spec.privacy_enabled:
            allowed = [
                {"participantIdentity": remote.identity, "allTracksAllowed": True}
                for remote in connection.room.remote_participants.values()
                if remote.identity.startswith(AI_SUBSCRIBER_PREFIXES)
            ]
            await _maybe_await(
                participant.set_track_subscription_permissions(False, allowed))
        else:
            # A camera that stops being private must stop being
            # restricted, or clearing the flag in the app does nothing.
            await _maybe_await(
                participant.set_track_subscription_permissions(True, []))
        connection.privacy_applied = connection.spec.privacy_enabled

    async def _enforce_audio(self, connection: _Connection) -> None:
        wants = connection.spec.audio_capture_enabled
        participant = connection.room.local_participant
        if wants and connection.audio_track is None:
            if self.audio_source_factory is None:
                return
            track = self.track_factory("audio", self.audio_source_factory())
            await participant.publish_track(track)
            connection.audio_track = track
        elif not wants and connection.audio_track is not None:
            await participant.unpublish_track(connection.audio_track)
            connection.audio_track.stop()
            connection.audio_track = None

    # ── frames ────────────────────────────────────────────────────
    async def pump(self) -> None:
        """Feed decoded frames to the room.

        Deliberately does NOT hold a queue of its own: the source already
        bounds and drops, and a second buffer here would be latency
        nobody asked for plus the unbounded-queue OOM again.
        """
        async for frame in self.source.video_frames():
            connection = self.connection
            if connection is not None:
                connection.video_source.capture_frame(frame)
                self.frames_published += 1

    async def close(self) -> None:
        await self._disconnect("shutting down")
        if self._capturing:
            await self.source.stop()
            self._capturing = False


async def _maybe_await(result: Any) -> Any:
    """`set_track_subscription_permissions` is a synchronous FFI call in
    the real SDK (the gateway calls it without await) and a coroutine in
    the fakes. Awaiting a plain value would be a TypeError, so decide at
    the call."""
    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
        return await result
    return result


def _default_track(kind: str, source: Any) -> Any:
    """Real LiveKit tracks. Imported lazily so the rules above stay
    testable on a machine with no LiveKit installed."""
    from livekit import rtc
    if kind == "video":
        return rtc.LocalVideoTrack.create_video_track("camera", source)
    return rtc.LocalAudioTrack.create_audio_track("camera-audio", source)
