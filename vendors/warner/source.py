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
import time

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

# A decoder that dies is restarted, but never faster than this. The
# reader loop notices death within 200ms, so an ffmpeg that cannot start
# at all would otherwise respawn five times a second forever.
RESTART_BACKOFF_SECONDS = 2.0

# LiveKit wants 48kHz PCM. The camera emits 8kHz AAC, so ffmpeg
# resamples on the way through — a few microseconds per packet.
AUDIO_RATE = 48000
AUDIO_CH = 1


def demuxer_for(enc: str) -> str:
    """ffmpeg's raw-elementary-stream demuxer for a vendor codec string.

    Punctuation is stripped before matching because the SDK, the config
    row and the page's heartbeat do not agree on spelling — "H265",
    "H.265" and "HEVC" all appear — and a spelling that fell through to
    the H.264 demuxer would decode exactly zero frames.
    """
    token = "".join(c for c in enc.upper() if c.isalnum())
    return "hevc" if token.startswith(("H265", "HEVC")) else "h264"


def stream_mismatch(detail: str, enc: str, width: int, height: int) -> str:
    """What the camera is really sending vs what the decoder was built for.

    The page's `frame_heartbeat` detail is "2304x1296 H265" — the vendor
    SDK's own view of the frames it just handed us, and the string that
    caught the sub-stream bug on 2026-08-21. The decoder cannot simply
    follow it: `video_frames` reads fixed-size chunks, so the output size
    is pinned and a surprise has to be REPORTED rather than absorbed.

    Returns "" when they agree, or when `detail` is not a heartbeat.
    """
    parts = detail.split()
    if len(parts) != 2 or "x" not in parts[0]:
        return ""
    size, codec = parts
    problems = []
    if size != f"{width}x{height}":
        problems.append(f"size {size}, decoder pinned to {width}x{height}")
    if demuxer_for(codec) != demuxer_for(enc):
        problems.append(f"codec {codec}, decoder configured for {enc}")
    return "; ".join(problems)


def video_argv(enc: str, fps: int, width: int, height: int) -> list[str]:
    """Encoded video on stdin, rawvideo on stdout.

    Deliberately NOT `-fflags +nobuffer`. That flag is on the gateway's
    AUDIO command and copying it here decoded exactly ZERO frames from a
    raw elementary stream — measured, not guessed. It is safe on a
    container carrying its own timing and wrong on a bare Annex-B pipe.

    `-fps_mode cfr` rather than the deprecated `-vsync cfr`. The vendor
    gives a millisecond stamp per frame but no container timing, and the
    publisher re-stamps presentation time from its own frame counter, so
    a constant output rate is what the consumer expects.

    NO DECODER TUNING HERE, and that is a measured conclusion rather
    than an oversight. ffmpeg was the largest per-camera memory item, so
    on 2026-08-21 the obvious suspects were each run against 90s of the
    live 3MP H.265 stream, paced at a real 15fps:

        -threads 1 / 2 / 4      no gain; `-f null` decode floor is 56MB
                                on 14 threads and 66-68MB capped, so
                                capping is slightly WORSE
        -thread_type slice      no gain, within run-to-run noise
        dropping -fps_mode cfr  no gain
        -filter_threads 1       no memory gain, ~0.15 CPU-s cheaper
        -max_muxing_queue_size,
        -muxing_queue_data_threshold,
        -thread_queue_size,
        -fflags +flush_packets,
        -avioflags direct       all inside the noise band

    The decoder was never the problem: it holds ~56MB, and the other
    ~180MB is output-side frames that pile up whenever the READER stops
    draining the pipe. That is fixed in `_Lane.start` instead.

    `-s` and `-pix_fmt` are NOT tuning knobs and must not be removed.
    Both pin the size of one unit on stdout, which is what the
    fixed-chunk reader depends on. Dropping `-pix_fmt` was measured as
    byte-identical output TODAY only because this camera sends
    yuvj420p, which differs from yuv420p by a range tag and not by a
    single byte; a 10-bit stream would silently double the frame size
    and desync the reader forever.
    """
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-flags", "low_delay",
        "-f", demuxer_for(enc), "-i", "pipe:0",
        "-fps_mode", "cfr", "-r", str(max(1, int(round(fps)))),
        # Pin the output size. Frames are read from stdout in fixed
        # width*height*3/2 chunks, so a camera streaming something other
        # than the configured size would desync the reader permanently
        # and every frame after the first would be garbage. When the
        # sizes already match — the normal case — ffmpeg skips the
        # scaler entirely.
        "-s", f"{width}x{height}",
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

    def __init__(self, name: str, camera_id: str, argv: list[str],
                 read_size: int):
        self.name = name
        self.camera_id = camera_id
        self.argv = argv
        # What one unit off stdout is. The readers below take exactly
        # this many bytes, and it also sets the pipe's flow-control
        # limit — see `start`.
        self.read_size = read_size
        self.proc: "asyncio.subprocess.Process | None" = None
        self.fed = 0
        self.dropped = 0
        self.restarts = 0
        # Whether this lane is SUPPOSED to be running. Without it the
        # readers below, which restart a dead decoder, would also
        # resurrect one that was deliberately stopped.
        self.wanted = False
        self._started_at = 0.0
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=MAX_QUEUED)
        self._tasks: list[asyncio.Task] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        """Idempotent while the decoder is ALIVE, not merely while
        `self.proc` is set.

        The guard here used to be `is not None`, and a crashed ffmpeg
        leaves a corpse: `proc` stays set with a `returncode`. Every
        later start() then returned immediately, so nothing could ever
        bring the lane back.
        """
        self.wanted = True
        if self.alive:
            return
        if self.proc is not None:
            await self.stop()
            self.wanted = True
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not on PATH")
        self._started_at = time.monotonic()
        log.info("[%s/%s] %s", self.camera_id, self.name, " ".join(self.argv))
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Sized to ONE unit of output. This is the only thing that
            # moved ffmpeg's resident memory at all (2026-08-21):
            #   controlled bench, 10 paired runs   239MB -> 187MB, 10/10
            #   live harness,     11 paired runs   232MB -> 206MB,  8/11
            # The live gain is smaller because the event loop there also
            # carries aiohttp and Playwright, so reader latency has
            # causes this cannot fix. CPU is unchanged: ffmpeg's own
            # cpu-seconds moved 1.540 -> 1.560 over 300 frames, about
            # two clock ticks.
            #
            # asyncio's default limit is 64KB. `feed_data` pauses the
            # pipe whenever the buffer passes 2x that, and `readexactly`
            # resumes it immediately because it needs a whole 4.5MB
            # frame — so a 3MP frame costs about 67 pause/resume cycles,
            # measured at 20,117 of them per 300 frames. Each pause
            # stops draining the pipe, ffmpeg blocks part-way through
            # writing a frame, and its filter and muxer queues back up.
            # AVBufferPool never releases, so the high-water mark of
            # that backlog is permanent RSS for the life of the process.
            # At one frame the buffer never reaches the pause threshold:
            # measured 0 pauses.
            #
            # This costs nothing on the Python side. `readexactly` has
            # to accumulate a whole frame either way; the limit only
            # decides whether the pipe is throttled while it does.
            limit=max(self.read_size, 64 * 1024),
        )
        self._tasks = [
            asyncio.create_task(self._pump()),
            asyncio.create_task(self._drain_stderr()),
        ]

    async def stop(self) -> None:
        self.wanted = False
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        # Drop what was queued for the process being killed. These NALUs
        # are from before the break, and feeding up to 120 of them to a
        # fresh decoder gets in ahead of the keyframe gate — the gate
        # only inspects what arrives AFTER the restart.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
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

    async def ensure_running(self) -> bool:
        """True once the decoder is up; restarts one that has died.

        THIS IS THE SUPERVISOR. There is no other: `CameraPublisher`
        calls `source.start()` once, when it begins capturing, and its
        `_capturing` flag then stays true for the life of the
        connection. Before this existed, an ffmpeg killed by a malformed
        stream — a firmware reboot mid-GOP is enough — left the camera
        publishing a frozen frame until it happened to leave the roster.

        Restarts are rate-limited so a decoder that cannot start at all
        does not respawn on every pass of a 200ms reader loop.
        """
        if self.alive:
            return True
        if not self.wanted:
            return False
        if time.monotonic() - self._started_at < RESTART_BACKOFF_SECONDS:
            return False
        if self.proc is not None:
            self.restarts += 1
            log.warning("[%s/%s] decoder exited rc=%s — restart %d",
                        self.camera_id, self.name, self.proc.returncode,
                        self.restarts)
        await self.start()
        return self.alive

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
        return {"alive": self.alive, "fed": self.fed, "restarts": self.restarts,
                "dropped": self.dropped, "queued": self._queue.qsize()}


class WarnerSource:
    """One physical camera: encoded in, decoded out.

    `video.proc.stdout` and `audio.proc.stdout` are read exactly as the
    gateway reads its RTSP ffmpeg's stdout.
    """

    def __init__(self, camera_id: str, enc: str = "H265", fps: int = 15,
                 width: int = 2304, height: int = 1296,
                 out_width: int = 0, out_height: int = 0):
        self.camera_id = camera_id
        self.enc = enc
        self.fps = fps
        # WHAT THE CAMERA SENDS versus WHAT WE PUBLISH. These were one
        # pair of numbers, and conflating them is what broke the stream
        # on 2026-08-25: `WARNER_CAPTURE_WIDTH` was set to 1920 to shrink
        # the picture, but that constant ALSO tells Chromium what to
        # capture, so it moved both ends at once and the mismatch check
        # started shouting about a camera that was behaving perfectly.
        #
        # `width/height` stay the camera's true output — that is what
        # `check_stream` compares against, and it must keep describing
        # reality or it reports a fault that does not exist.
        self.width = width
        self.height = height
        # `out_*` is what ffmpeg SCALES to and what we publish. Default
        # is the camera's own size, so this changes nothing unless asked.
        self.out_width = out_width or width
        self.out_height = out_height or height
        # ONE definition of a frame's size, shared by the reader and the
        # pipe limit. It used to be recomputed in `video_frames`, which
        # is the kind of duplication that eventually disagrees — and
        # disagreeing here desyncs the stream permanently.
        #
        # Derived from the OUTPUT size, which is the only size that ever
        # appears on stdout: `-s` makes ffmpeg emit exactly this, whatever
        # arrives. The reader therefore cannot desync from a scale.
        self.frame_size = self.out_width * self.out_height * 3 // 2
        self.video = _Lane(
            "video", camera_id,
            video_argv(enc, fps, self.out_width, self.out_height),
            self.frame_size)
        # The facilities audio kill is decided upstream. When audio is
        # not wanted the lane is never created, so there is no
        # muted-but-open path to leak from.
        self.audio: _Lane | None = None
        self.started = False        # seen a keyframe yet?
        self._mismatch = ""         # last one reported, so it is said once

    async def start(self, want_audio: bool = True) -> None:
        await self.video.start()
        await self.set_audio(want_audio)

    async def set_audio(self, enabled: bool) -> None:
        """Open or cut the audio lane while video keeps running.

        Its own ffmpeg process, so this never disturbs the picture — the
        reason the lane was split out in the first place.
        """
        if enabled and self.audio is None:
            self.audio = _Lane("audio", self.camera_id, audio_argv(),
                               AUDIO_RATE * AUDIO_CH * 2 // 50)
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

    def require_keyframe(self) -> None:
        """Wait for the next IDR before feeding the decoder again.

        Called when the frame socket closes. The stall watchdog recreates
        the PAGE without touching this object, so `started` stayed true
        across the gap and the first frame of the new session — a
        P-frame, unless the reconnect happened to land on a keyframe —
        went into a decoder still holding references the camera had
        already thrown away.
        """
        self.started = False

    def check_stream(self, detail: str) -> str:
        """Report, once, when the camera is not sending what we decode.

        Deliberately does NOT re-pin the decoder to what arrived. The
        output size is what `video_frames` reads in fixed chunks, so
        following the camera would trade a loud failure for a silent one
        — every frame after the first shifted by the remainder, forever.
        The heartbeat repeats every 5s, so only a CHANGED problem is
        logged; a line per heartbeat would bury it.
        """
        problem = stream_mismatch(detail, self.enc, self.width, self.height)
        if problem and problem != self._mismatch:
            log.error("[%s] STREAM MISMATCH: %s — this does not recover on "
                      "its own, the camera or its inventory row is wrong",
                      self.camera_id, problem)
        self._mismatch = problem
        return problem

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

    async def video_frames(self):
        """Raw yuv420p frames off the decoder's stdout, forever.

        `readexactly` rather than `read`: one frame is exactly
        width*height*3/2 bytes and a short read would shift every
        subsequent frame by the remainder.

        This loop is ALSO where a dead decoder is restarted. It used to
        say a supervisor above did that; there was none, and a camera
        whose ffmpeg died stayed frozen until it left the roster. The
        restart re-arms the keyframe gate first, because handing a fresh
        decoder a P-frame produces exactly the garbage the gate exists
        to prevent.
        """
        size = self.frame_size
        while True:
            if not self.video.alive:
                self.started = False
                if not await self.video.ensure_running():
                    await asyncio.sleep(0.2)
                    continue
            proc = self.video.proc
            if proc is None or proc.returncode is not None or proc.stdout is None:
                await asyncio.sleep(0.2)
                continue
            try:
                yield await proc.stdout.readexactly(size)
            except asyncio.IncompleteReadError:
                log.warning("[%s] decoder ended mid-frame", self.camera_id)
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise

    async def audio_frames(self):
        """48kHz mono s16le PCM off the audio decoder, forever.

        20ms at a time, which is what LiveKit expects per frame. Yields
        nothing at all while the lane is cut, which is the normal state
        at a site that does not permit recording — and restarts the lane
        when it dies while still wanted, for the same reason video does.
        """
        while True:
            lane = self.audio
            chunk = lane.read_size if lane else 0    # 20ms
            if lane is not None and not lane.alive:
                await lane.ensure_running()
            proc = lane.proc if lane else None
            if proc is None or proc.returncode is not None or proc.stdout is None:
                await asyncio.sleep(0.2)
                continue
            try:
                yield await proc.stdout.readexactly(chunk)
            except asyncio.IncompleteReadError:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise

    def status(self) -> dict:
        return {
            "enc": self.enc, "fps": self.fps,
            "size": f"{self.width}x{self.height}",
            "started_on_keyframe": self.started,
            "video": self.video.status(),
            "audio": self.audio.status() if self.audio else None,
        }
