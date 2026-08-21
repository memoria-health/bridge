#!/usr/bin/env python3
"""Warner frames and audio, decoded for `rtc.VideoSource`/`AudioSource`.

WHY THIS SHAPE

`nemara-gateway/publisher.py` publishes from Python through
`rtc.VideoSource` as an ordinary `cam_*` participant. That matters
twice: no LiveKit Ingress (so no per-minute ingest bill), and no new
participant identity for the webhooks and `room_reconciler` to trip
over. We take that idea and nothing else — the gateway pulls RTSP, we
cannot, because the vendor's transport is JS + WASM with no native SDK.

So the browser stays, but only as transport. It hands us the encoded
frames the vendor SDK was already producing (ENCODED_PASSTHROUGH.md:
H.265 Annex-B, VPS/SPS in-band on keyframes, ~190 Kbps, plus AAC in
ADTS). Deleted per camera: a WASM HEVC decoder and its heap, a 3MP
canvas and its rasterisation, and a browser WebRTC encoder. Added: one
native ffmpeg decode. The encode moves to the LiveKit SDK.

    browser (P2P only) --NALUs--> ffmpeg --raw--> rtc.VideoSource
                       --ADTS---> ffmpeg --PCM--> rtc.AudioSource

Video output is byte-identical to the gateway's `build_ffmpeg_cmd`
(rawvideo, yuv420p, constant rate), so the frame reader, stall watchdog
and motion sampling downstream are reused rather than re-derived.

WHY TWO PROCESSES, not one. Video and audio arrive as separate vendor
callbacks with separate framing; muxing them into one ffmpeg input
would mean inventing a container for no gain. Two pipes also means
audio failing cannot take video with it — and video is the one a
caregiver is watching.
"""
from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("warner.source")

# Frame-type byte the runtime page prepends, matching the vendor's own
# constants (PROC_FRAME_TYPE_AUDIO / _IFRAME / _PFRAME).
FRAME_AUDIO = 0
FRAME_KEY = 1
FRAME_DELTA = 2

# Bound the hand-off queues. The identity OOM (2026-08-15) was unbounded
# stream queues; a live view wants the newest picture, so drop the
# oldest rather than grow.
MAX_QUEUED = 120

# LiveKit wants 48kHz PCM. The camera emits 8kHz AAC, so ffmpeg
# resamples on the way through — a few microseconds per packet.
AUDIO_RATE = 48000
AUDIO_CH = 1


def video_argv(enc: str, fps: int) -> list[str]:
    """Encoded video on stdin, rawvideo on stdout.

    Deliberately NOT `-fflags +nobuffer`. That flag is on the gateway's
    AUDIO command and copying it here decoded exactly ZERO frames from a
    raw elementary stream — measured, not guessed. It is safe on a
    container carrying its own timing and wrong on a bare Annex-B pipe.

    `-fps_mode cfr` rather than the deprecated `-vsync cfr`. The vendor
    gives a millisecond stamp per frame but no container timing, and the
    publisher re-stamps presentation time from its own frame counter, so
    a constant output rate is what the consumer expects.
    """
    fmt = "hevc" if enc.upper().startswith("H265") else "h264"
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-flags", "low_delay",
        "-f", fmt, "-i", "pipe:0",
        "-fps_mode", "cfr", "-r", str(max(1, int(round(fps)))),
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-",
    ]


def audio_argv() -> list[str]:
    """ADTS AAC on stdin, 48kHz mono s16le PCM on stdout.

    `-f aac` is the ADTS demuxer; the camera's packets start `ff f1`,
    which is an ADTS sync word, so this is exactly the right reader.
    """
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-flags", "low_delay",
        "-f", "aac", "-i", "pipe:0",
        "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CH),
        "-f", "s16le", "-",
    ]


class _Lane:
    """One ffmpeg decode: bytes in on a queue, decoded bytes on stdout.

    Failure is contained here. A lane that dies is reported and can be
    restarted by the caller without disturbing its sibling.
    """

    def __init__(self, name: str, camera_id: str, argv: list[str]):
        self.name = name
        self.camera_id = camera_id
        self.argv = argv
        self.proc: "asyncio.subprocess.Process | None" = None
        self.fed = 0
        self.dropped = 0
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=MAX_QUEUED)
        self._tasks: list[asyncio.Task] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        if self.proc is not None:
            return
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not on PATH")
        log.info("[%s/%s] %s", self.camera_id, self.name, " ".join(self.argv))
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._tasks = [
            asyncio.create_task(self._pump()),
            asyncio.create_task(self._drain_stderr()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        if self.proc:
            try:
                if self.proc.stdin and not self.proc.stdin.is_closing():
                    self.proc.stdin.close()
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
            self.proc = None

    def offer(self, body: bytes) -> None:
        """Never blocks. Drops the OLDEST under pressure — a live view
        wants the newest picture, and a backlog is latency nobody asked
        for."""
        if not body:
            return
        self.fed += 1
        try:
            self._queue.put_nowait(body)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(body)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            self.dropped += 1
            if self.dropped % 100 == 1:
                log.warning("[%s/%s] backlog, dropped=%d",
                            self.camera_id, self.name, self.dropped)

    async def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        try:
            while True:
                body = await self._queue.get()
                self.proc.stdin.write(body)
                await self.proc.stdin.drain()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a dead decoder must not kill the loop
            log.exception("[%s/%s] stdin failed", self.camera_id, self.name)

    async def _drain_stderr(self) -> None:
        """ffmpeg's complaints are the only diagnosis when a stream will
        not start, and an unread stderr pipe eventually blocks ffmpeg."""
        if not self.proc or not self.proc.stderr:
            return
        try:
            async for line in self.proc.stderr:
                text = line.decode(errors="replace").strip()
                if text:
                    log.warning("[%s/%s] ffmpeg: %s",
                                self.camera_id, self.name, text)
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict:
        return {"alive": self.alive, "fed": self.fed,
                "dropped": self.dropped, "queued": self._queue.qsize()}


class WarnerSource:
    """One physical camera: encoded in, decoded out.

    `video.proc.stdout` and `audio.proc.stdout` are read exactly as the
    gateway reads its RTSP ffmpeg's stdout.
    """

    def __init__(self, camera_id: str, enc: str = "H265", fps: int = 15,
                 width: int = 2304, height: int = 1296):
        self.camera_id = camera_id
        self.enc = enc
        self.fps = fps
        self.width = width
        self.height = height
        self.video = _Lane("video", camera_id, video_argv(enc, fps))
        # The facilities audio kill is decided upstream. When audio is
        # not wanted the lane is never created, so there is no
        # muted-but-open path to leak from.
        self.audio: _Lane | None = None
        self.started = False        # seen a keyframe yet?

    async def start(self, want_audio: bool = True) -> None:
        await self.video.start()
        await self.set_audio(want_audio)

    async def set_audio(self, enabled: bool) -> None:
        """Open or cut the audio lane while video keeps running.

        Its own ffmpeg process, so this never disturbs the picture — the
        reason the lane was split out in the first place.
        """
        if enabled and self.audio is None:
            self.audio = _Lane("audio", self.camera_id, audio_argv())
            await self.audio.start()
            log.info("[%s] audio lane opened", self.camera_id)
        elif not enabled and self.audio is not None:
            lane, self.audio = self.audio, None
            await lane.stop()
            log.info("[%s] audio lane cut", self.camera_id)

    async def stop(self) -> None:
        await self.video.stop()
        await self.set_audio(False)
        self.started = False

    def offer(self, payload: bytes) -> None:
        """One framed message from the runtime page: type byte + body."""
        if not payload:
            return
        kind, body = payload[0], payload[1:]
        if not body:
            return
        if kind == FRAME_AUDIO:
            if self.audio:
                self.audio.offer(body)
            return
        # Never begin mid-GOP. Without the parameter sets and an IDR the
        # decoder emits garbage, and a caregiver seeing a corrupted
        # picture is worse than seeing none. VPS/SPS ride in-band on
        # keyframes here, so waiting costs at most one GOP.
        if not self.started:
            if kind != FRAME_KEY:
                return
            self.started = True
            log.info("[%s] first keyframe — decoding", self.camera_id)
        self.video.offer(body)

    def status(self) -> dict:
        return {
            "enc": self.enc, "fps": self.fps,
            "started_on_keyframe": self.started,
            "video": self.video.status(),
            "audio": self.audio.status() if self.audio else None,
        }
