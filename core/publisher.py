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
# How often a RESTRICTIVE permission set is re-sent even when
# unchanged. Self-heals a permission the SFU dropped without
# resetting every subscriber's layer every two seconds.
_PRIVACY_REASSERT_S = 30.0

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
        publish_options_factory: Callable[[int, int], Any] | None = None,
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
        self.publish_options_factory = (
            publish_options_factory or _default_publish_options)
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
        # Last permission set actually sent, so an unchanged one is not
        # re-sent every two seconds. None = nothing sent yet.
        self._privacy_sent: tuple | None = None
        self._privacy_sent_at = 0.0

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
            # EXPLICIT ENCODING, not the SDK defaults. The gateway learned
            # this on 2026-07-27 and the bridge never got the fix: with no
            # options the SDK picks a bitrate for the resolution, and for a
            # 2304x1296 source that target is far below what the picture
            # needs. libwebrtc's rate control answers a starved target by
            # dropping RESOLUTION, so the stream oscillates.
            #
            # Measured 2026-08-24, this camera, arriving at the identity
            # service: 2304x1296 -> 320x180 -> 2304x1296 -> 320x180, three
            # or four collapses a minute, while the gateway's Tapo never
            # left 1920x1080 once. Everything downstream followed it —
            # nothing is detectable at 320x180, so tracks cleared, the
            # room read as empty, and the picture went soft.
            #
            # NOT a CPU limit: the publisher sat at 29-68% of ONE core on a
            # 14-core machine throughout, and the camera fed it a steady
            # 15fps. The encoder was not struggling; it was under-targeted.
            #
            # 5 Mbps because this source is 44% more pixels than the
            # gateway's 1080p at 4 Mbps. max_framerate follows the source
            # rather than a constant: this vendor is 15fps and a wrong
            # value here is what CFR output would be re-timed against.
            await room.local_participant.publish_track(
                track,
                self.publish_options_factory(
                    int(max(1, round(self.source.fps or 15))),
                    _VIDEO_MAX_BITRATE,
                ),
            )
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
        # A fresh room has no memory of what we last sent, so the next
        # _apply_privacy must actually send rather than skip as
        # unchanged. Forgetting here is what stops the change-guard
        # turning into a permanently-unenforced camera.
        self._privacy_sent = None
        self._privacy_sent_at = 0.0
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
        # ONLY WHEN IT CHANGES. This runs on every reconcile pass — every
        # two seconds — and used to re-send the permission set each time
        # even when nothing had moved. Every one of those calls makes the
        # SFU re-evaluate the publisher's tracks, and a subscriber's
        # track settings do not survive that: they fall back to the
        # SDK default, which the SFU reads as the SMALLEST layer.
        #
        # Measured 2026-08-24 on the identity service: 2304x1296 ->
        # 320x180 and back, over and over, ~14% of the time spent
        # collapsed. At 320x180 there is nothing to detect, so tracks
        # cleared and the room read as empty every time it happened.
        # The gateway re-applies only on connect or change and its
        # camera never moved off 1920x1080 once — that was the whole
        # difference between the two.
        #
        # "Enforcement that happens once is not enforcement" still
        # holds, and is why the DESIRED set is recomputed every pass: a
        # worker that joins later changes it and is applied immediately.
        # Re-sending an IDENTICAL set is not enforcement, it is churn.
        want_all = not spec.privacy_enabled
        allow_ids = tuple(sorted(
            remote.identity
            for remote in room.remote_participants.values()
            if remote.identity.startswith(AI_SUBSCRIBER_PREFIXES)
        )) if spec.privacy_enabled else ()
        now = time.time()
        unchanged = self._privacy_sent == (want_all, allow_ids)
        # A RESTRICTION that the SFU drops is a privacy leak, so that one
        # keeps healing itself — just at 30s instead of every 2s. An
        # allow-all that gets dropped leaves the DEFAULT, which is
        # allow-all, so re-sending it protects nothing and costs the
        # subscriber its layer every time.
        due = (not want_all) and (now - self._privacy_sent_at >= _PRIVACY_REASSERT_S)
        if unchanged and not due:
            return
        self._privacy_sent = (want_all, allow_ids)
        self._privacy_sent_at = now
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


# 5 Mbps: this vendor's source is 2304x1296, 44% more pixels than the
# gateway's 1080p at 4 Mbps.
_VIDEO_MAX_BITRATE = 5_000_000


def _default_publish_options(max_framerate: int, max_bitrate: int) -> Any:
    """Real LiveKit publish options. Lazy import for the same reason as
    the track factory — these rules stay testable without LiveKit."""
    from core.livekit_adapters import publish_options
    return publish_options(max_framerate, max_bitrate)


async def encoder_stats(conn: Any) -> dict:
    """What WebRTC itself says about the outgoing video.

    `quality_limitation_reason` is the only authoritative answer to "why
    did the resolution drop" — cpu, bandwidth, or none. Without it every
    explanation is a guess, and on 2026-08-24 four successive guesses
    (the SFU choosing, a subscriber's pin, bursty source frames, a
    starved bitrate target) were each measured and each wrong while the
    stream kept collapsing 2304x1296 -> 320x180 several times a minute.
    """
    track = getattr(conn, "video_track", None) if conn else None
    if track is None or not hasattr(track, "get_stats"):
        return {}
    try:
        out: dict = {}
        for st in await track.get_stats():
            rid = str(getattr(st, "rid", "") or getattr(st, "id", "?"))
            row = {}
            for field in ("quality_limitation_reason", "frame_width",
                          "frame_height", "frames_per_second",
                          "target_bitrate", "frames_encoded"):
                val = getattr(st, field, None)
                if val is not None:
                    row[field] = str(val)[:60]
            if row:
                out[rid[:40]] = row
        return out
    except Exception as exc:  # noqa: BLE001 — telemetry, never fatal
        return {"error": f"{type(exc).__name__}: {exc}"[:120]}


async def encoder_stats_raw(conn: Any) -> Any:
    """Unfiltered, for one diagnosis. My field-name guesses returned an
    empty dict and an empty dict is indistinguishable from "no limit"."""
    track = getattr(conn, "video_track", None) if conn else None
    if track is None or not hasattr(track, "get_stats"):
        return {"no_track": True}
    try:
        out = []
        for st in await track.get_stats():
            # protobuf with a oneof: ListFields() yields only what is
            # actually set. vars() returns the class attributes, which is
            # why the first read came back as a list of type names.
            for desc, val in st.ListFields():
                if desc.name not in ("outbound_rtp",):
                    continue
                row = {}
                for f, v in val.ListFields():
                    if hasattr(v, "ListFields"):
                        for f2, v2 in v.ListFields():
                            if f2.name in (
                                "rid", "frame_width", "frame_height",
                                "frames_per_second", "target_bitrate",
                                "quality_limitation_reason",
                                "frames_encoded", "active",
                            ):
                                row[f2.name] = str(v2)[:40]
                out.append(row)
        return out[:8]
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:160]}


def _default_permission(identity: str) -> Any:
    from core.livekit_adapters import permission
    return permission(identity)


def _default_track(kind: str, source: Any) -> Any:
    """Real LiveKit tracks. Imported lazily so the rules above stay
    testable on a machine with no LiveKit installed."""
    from core.livekit_adapters import track
    return track(kind, source)
