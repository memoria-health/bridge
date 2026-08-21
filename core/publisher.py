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
import time
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
        self.audio_source: Any = None
        # publish_track returns a LocalTrackPublication, and
        # unpublish_track takes its SID — a string, not the track.
        self.audio_publication: Any = None
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
        permission_factory: Callable[[str], Any] | None = None,
    ):
        self.camera_id = camera_id
        self.source = source
        self.livekit_url = livekit_url
        self.token_for = token_for
        self.room_factory = room_factory
        self.video_source_factory = video_source_factory
        self.audio_source_factory = audio_source_factory
        self.track_factory = track_factory or _default_track
        self.permission_factory = permission_factory or _default_permission
        self.connection: _Connection | None = None
        # Observed effect, for the fleet report. Empty until something
        # has actually been applied.
        self.applied: dict[str, Any] = {}
        self.frames_published = 0
        self.last_published_at = 0.0
        self._capturing = False
        # What the SOURCE's audio lane is currently set to; None
        # while not capturing.
        self._audio_open: bool | None = None

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

        # Connect BEFORE capturing. Starting the capture first meant that
        # while LiveKit was unreachable, ffmpeg and the browser page were
        # started and stopped on every 2s pass — thrash, and a camera
        # that looks alive in `ps` while publishing nothing.
        if wanted is not None and self.connection is None:
            await self._connect(wanted)

        if self.connection is None:
            await self._stop_capture()
            return

        if not self._capturing:
            await self.source.start(want_audio=wanted.audio_capture_enabled)
            self._audio_open = wanted.audio_capture_enabled
            self._capturing = True

        # NOT republished when privacy turns on. Measured against a real
        # SFU, set_track_subscription_permissions gates NEW subscriptions
        # and does not revoke existing ones, so a caregiver already
        # watching keeps the track until they leave the room. Republishing
        # would drop them — and would ALSO drop the detector and the
        # identity service, blinding the AI for several seconds.
        #
        # That trade is backwards. The product promise is that a private
        # room keeps producing AI alerts while no human sees the video, so
        # interrupting the AI to enforce a rule the app already enforces
        # in its live view costs the thing that matters to protect the
        # thing that does not. The permission set below still gates every
        # NEW subscriber, which is the durable half.
        # EVERY pass, including when the room was already connected
        # before this call. Enforcement that happens once is not
        # enforcement.
        self.connection.spec = wanted
        await self._enforce(self.connection)

        if self.connection is None:
            await self._stop_capture()

    async def _stop_capture(self) -> None:
        if self._capturing:
            await self.source.stop()
            self._capturing = False
            self._audio_open = None

    async def _connect(self, spec: Publisher) -> None:
        room = self.room_factory()
        try:
            # The token may be minted remotely, so this can be a
            # coroutine; the fakes hand back a plain string.
            token = await _maybe_await(
                self.token_for(spec.identity, spec.room))
            await room.connect(self.livekit_url, token)
            # BEFORE the track exists. Applying privacy after publishing
            # leaves a window in which a subscriber can auto-subscribe to
            # a bathroom camera — bounded by a subprocess spawn and a
            # server round-trip rather than by anything designed. The
            # permission is participant-level, so it can be set first.
            await self._apply_privacy(room, spec)
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
        self.applied = {"desired_state": "disconnected", "reason": why}
        self.last_published_at = 0.0
        try:
            await connection.room.disconnect()
        except Exception:  # noqa: BLE001 — already gone
            pass
        for track in (connection.video_track, connection.audio_track):
            # `stop()` is a browser-SDK idiom; the Python SDK's
            # LocalVideoTrack has no such method and releases with the
            # room. Calling it unconditionally raised inside teardown,
            # which is the worst place to raise — the room disconnect
            # above had already happened, so the publisher was left
            # half-torn-down with self.connection cleared.
            stop = getattr(track, "stop", None)
            if callable(stop):
                stop()
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
        try:
            await self._enforce_audio(connection)
        except Exception as exc:  # noqa: BLE001
            # Same reasoning as privacy. An unpublish that raises against
            # a room that reconnected under us left audio_track set, so
            # EVERY later pass raised at the same line and this camera —
            # and, before the caller isolated them, every camera after it
            # in the plan — stopped being reconciled at all.
            log(f"[{self.camera_id}] audio enforcement failed for "
                f"{connection.spec.identity}: {exc}; tearing down")
            await self._disconnect("audio unenforceable")
            return
        self._note_applied(connection)

    def _note_applied(self, connection: _Connection) -> None:
        """What actually TOOK EFFECT, not what was asked for.

        The contract calls this the difference between "the backend sent
        disconnect" and "the camera stopped publishing", so reading it
        off the plan would make it a restatement of intent. Every value
        here is observed: privacy only after the SFU accepted the
        permission call, audio only if a track is genuinely published.
        """
        self.applied = {
            "desired_state": "connected",
            "room": connection.spec.room,
            "privacy_enabled": connection.privacy_applied,
            "audio_capture_enabled": connection.audio_track is not None,
        }

    async def _enforce_privacy(self, connection: _Connection) -> None:
        await self._apply_privacy(connection.room, connection.spec)
        connection.privacy_applied = connection.spec.privacy_enabled

    async def _apply_privacy(self, room: Any, spec: Publisher) -> None:
        """Allow-list the AI participants ACTUALLY in the room.

        LiveKit matches a permission entry by exact participant identity,
        so a prefix string allow-lists nobody. `runtime.js` passed the
        literal "detector_worker" and therefore made a private camera
        invisible to the AI as well as to people. Rebuilt every pass,
        which is also how a worker that joins later gets allowed.
        """
        participant = room.local_participant
        if spec.privacy_enabled:
            allowed = [
                self.permission_factory(remote.identity)
                for remote in room.remote_participants.values()
                if remote.identity.startswith(AI_SUBSCRIBER_PREFIXES)
            ]
            # KEYWORDS. The SDK call is keyword-only, so the positional
            # form raised a TypeError on every private camera — which
            # this code turns into a tear-down, so a bathroom camera
            # published nothing at all rather than publishing wrongly.
            # Safe direction, still completely broken.
            await _maybe_await(participant.set_track_subscription_permissions(
                allow_all_participants=False, participant_permissions=allowed))
        else:
            # A camera that stops being private must stop being
            # restricted, or clearing the flag in the app does nothing.
            await _maybe_await(participant.set_track_subscription_permissions(
                allow_all_participants=True, participant_permissions=[]))

    async def _enforce_audio(self, connection: _Connection) -> None:
        wants = connection.spec.audio_capture_enabled
        participant = connection.room.local_participant
        # Cut or open the lane in the SOURCE, not only the track. The
        # kill is described as severed at the source, and a decoder
        # still being fed the microphone is not severed. Opening it
        # matters just as much: want_audio used to be decided once at
        # capture start, so a site enabling audio later got a published
        # track backed by a lane that was never opened — silent, and
        # indistinguishable from working audio.
        # Order matters on a KILL: unpublish first, then cut the lane.
        # Cutting first leaves already-decoded PCM able to reach LiveKit
        # through pump_audio, because the track is still published.
        if not wants and connection.audio_track is not None:
            # By SID. Passing the track object raised "bad argument type
            # for built-in operation" against the real SDK, so the
            # facilities audio kill failed EVERY time — and with
            # tear-down-on-failure that meant the camera dropped and
            # republished in a loop, with audio, at a site that does not
            # permit recording it.
            sid = getattr(connection.audio_publication, "sid", None)
            if sid:
                await participant.unpublish_track(sid)
            stop = getattr(connection.audio_track, "stop", None)
            if callable(stop):
                stop()
            connection.audio_track = None
            connection.audio_source = None
            connection.audio_publication = None
        if wants != self._audio_open:
            setter = getattr(self.source, "set_audio", None)
            if setter is not None:
                await setter(wants)
            self._audio_open = wants
        if wants and connection.audio_track is None:
            if self.audio_source_factory is None:
                return
            audio_source = self.audio_source_factory()
            track = self.track_factory("audio", audio_source)
            connection.audio_publication = await participant.publish_track(track)
            connection.audio_source = audio_source
            connection.audio_track = track

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
                # The ONLY evidence that the whole chain works. Frames
                # arriving at the browser proves the camera is alive; this
                # proves they reached LiveKit.
                self.last_published_at = time.time()

    async def pump_audio(self) -> None:
        """PCM into LiveKit, only while an audio track is published.

        Read unconditionally rather than only when wanted: the decoder's
        stdout is a pipe, and a lane nobody drains fills and blocks
        ffmpeg. The lane itself is cut at the source when the kill
        arrives, so there is normally nothing here to read.
        """
        frames = getattr(self.source, "audio_frames", None)
        if frames is None:
            return
        async for chunk in frames():
            connection = self.connection
            if connection is None or connection.audio_track is None:
                continue
            await connection.audio_source.capture_frame(chunk)

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


def _default_permission(identity: str) -> Any:
    from core.livekit_adapters import permission
    return permission(identity)


def _default_track(kind: str, source: Any) -> Any:
    """Real LiveKit tracks. Imported lazily so the rules above stay
    testable on a machine with no LiveKit installed."""
    from core.livekit_adapters import track
    return track(kind, source)
