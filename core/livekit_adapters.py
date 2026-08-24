#!/usr/bin/env python3
"""The only place that imports LiveKit's rtc SDK.

`core/publisher.py` takes its LiveKit objects as factories so its rules
can be tested with no network and no SDK installed. These are the real
implementations it gets in production. They adapt raw decoded bytes into
the frame objects the SDK wants, so nothing above has to know the shape.
"""
from __future__ import annotations

from typing import Any

AUDIO_RATE = 48000
AUDIO_CHANNELS = 1


def room() -> Any:
    from livekit import rtc
    return rtc.Room()


class VideoSource:
    """Raw yuv420p bytes in, LiveKit video frames out."""

    def __init__(self, width: int, height: int):
        from livekit import rtc
        self._rtc = rtc
        self.width = width
        self.height = height
        self.lk = rtc.VideoSource(width, height)

    def capture_frame(self, raw: bytes) -> None:
        self.lk.capture_frame(self._rtc.VideoFrame(
            self.width, self.height, self._rtc.VideoBufferType.I420, raw))


class AudioSource:
    """Raw 48kHz mono s16le PCM in, LiveKit audio frames out."""

    def __init__(self) -> None:
        from livekit import rtc
        self._rtc = rtc
        self.lk = rtc.AudioSource(AUDIO_RATE, AUDIO_CHANNELS)

    async def capture_frame(self, pcm: bytes) -> None:
        # 2 bytes per sample, one channel.
        samples = len(pcm) // 2
        if not samples:
            return
        await self.lk.capture_frame(self._rtc.AudioFrame(
            data=pcm, sample_rate=AUDIO_RATE,
            num_channels=AUDIO_CHANNELS, samples_per_channel=samples))


def permission(identity: str) -> Any:
    """One allow-list entry, in the type the SDK actually wants.

    A dict is silently wrong: the call is keyword-only and takes
    `ParticipantTrackPermission` objects, so passing dicts positionally
    raised on every private camera. `allow_all` covers both the video and
    the audio track for that participant.
    """
    from livekit import rtc
    return rtc.ParticipantTrackPermission(
        participant_identity=identity, allow_all=True)


def track(kind: str, source: Any) -> Any:
    """A LiveKit track over one of the adapters above.

    `.lk` is the real SDK object; the adapter is only the byte shim.
    """
    from livekit import rtc
    underlying = getattr(source, "lk", source)
    if kind == "video":
        return rtc.LocalVideoTrack.create_video_track("camera", underlying)
    return rtc.LocalAudioTrack.create_audio_track("camera-audio", underlying)


def publish_options(max_framerate: int, max_bitrate: int):
    """Explicit video encoding for a published track.

    With no options the SDK picks a bitrate from the resolution, and for
    a 2304x1296 source that target is far below what the picture needs.
    libwebrtc answers a starved target by dropping RESOLUTION, so the
    stream oscillates — measured 2026-08-24 as 2304x1296 -> 320x180 and
    back, three or four times a minute, while the gateway's Tapo (which
    HAS pinned its encoding since 2026-07-27) never moved once.
    """
    from livekit import rtc

    return rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_CAMERA,
        video_encoding=rtc.VideoEncoding(
            max_framerate=max_framerate, max_bitrate=max_bitrate,
        ),
    )
