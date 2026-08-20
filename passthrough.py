#!/usr/bin/env python3
"""Publish the camera's OWN encoded frames, instead of re-making them.

The vendor SDK hands the runtime page an encoded elementary stream on
every frame (see ENCODED_PASSTHROUGH.md). The canvas route decodes that
in WebAssembly, paints it, and re-encodes it — roughly 800MB and a CPU
core per camera, spent turning ~190 Kbps of H.265 into megabits of
something worse.

This receives those bytes over a WebSocket from the page and pipes them
into ffmpeg, which publishes to LiveKit. ffmpeg is the seam that makes
the codec question stop mattering:

    H.264 in    -> `-c:v copy`      no decode, no encode anywhere
    H.265 in    -> `-c:v libx264`   a transcode, but no WASM decoder, no
                                    3MP canvas, no browser encoder

The second is the worse case and it is still several times cheaper than
today. Neither path blocks the other, so the build does not wait on
knowing which one we are on.

SAFETY: this is the video path of a healthcare product. It is behind a
flag, the canvas route is untouched when the flag is off, and a stream
only starts on a keyframe — beginning mid-GOP would hand a decoder
garbage and show a caregiver a broken picture.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field

log = logging.getLogger("passthrough")

# Frame type byte the page prepends (matches the vendor's constants).
FRAME_KEY = 1
FRAME_DELTA = 2

# Stop an unbounded queue becoming unbounded memory — the failure mode
# the identity OOM work already paid for once. Drop oldest instead.
MAX_QUEUED_FRAMES = 120


@dataclass
class Passthrough:
    """One camera's encoded lane: page -> ffmpeg -> LiveKit."""

    camera_id: str
    whip_url: str
    enc: str = "H265"
    transcode: bool = True
    proc: "asyncio.subprocess.Process | None" = None
    started: bool = False
    frames_in: int = 0
    frames_dropped: int = 0
    _queue: "asyncio.Queue[bytes]" = field(
        default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUED_FRAMES))
    _pump: "asyncio.Task | None" = None

    # ── lifecycle ─────────────────────────────────────────────────
    def ffmpeg_argv(self) -> list[str]:
        """Annex-B on stdin, LiveKit out.

        `-c:v copy` is the whole point when the input codec is already
        acceptable: ffmpeg then never touches a pixel, it only
        re-containers. The transcode branch exists so an H.265 camera is
        still cheaper than the canvas route, not because it is the goal.

        Output format follows the URL rather than being hardcoded. WHIP
        is the direct route but is missing from stock builds (the
        Homebrew ffmpeg 7.1.1 here has no `whip` muxer), so RTMP into
        LiveKit Ingress is the default — it works with any ffmpeg and
        carries HEVC via enhanced RTMP, which this build does support.
        """
        fmt = "hevc" if self.enc.upper().startswith("H265") else "h264"
        video = (["-c:v", "libx264", "-preset", "veryfast", "-tune",
                  "zerolatency", "-pix_fmt", "yuv420p"]
                 if self.transcode else ["-c:v", "copy"])
        out_fmt = "whip" if self.whip_url.startswith("http") else "flv"
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts",
            "-f", fmt, "-i", "pipe:0",
            *video,
            "-an",
            "-f", out_fmt, self.whip_url,
        ]

    async def start(self) -> None:
        if self.proc is not None:
            return
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not on PATH — passthrough needs it")
        argv = self.ffmpeg_argv()
        log.info("[%s] passthrough start: %s", self.camera_id, " ".join(argv))
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._pump = asyncio.create_task(self._pump_stdin())
        asyncio.create_task(self._drain_stderr())

    async def stop(self) -> None:
        if self._pump:
            self._pump.cancel()
            self._pump = None
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
        self.started = False

    # ── data path ─────────────────────────────────────────────────
    def offer(self, payload: bytes) -> None:
        """Take one framed message from the page. Never blocks."""
        if not payload:
            return
        frame_type, body = payload[0], payload[1:]
        if not body:
            return
        # Never begin mid-GOP. Without the parameter sets and an IDR a
        # decoder emits garbage, and the caregiver sees a broken image
        # rather than nothing — which is the worse of the two.
        if not self.started:
            if frame_type != FRAME_KEY:
                return
            self.started = True
            log.info("[%s] first keyframe — publishing", self.camera_id)
        self.frames_in += 1
        try:
            self._queue.put_nowait(body)
        except asyncio.QueueFull:
            # Drop the OLDEST: a live view wants the newest picture, and
            # a backlog is latency nobody asked for.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(body)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            self.frames_dropped += 1
            if self.frames_dropped % 100 == 1:
                log.warning("[%s] passthrough backlog, dropped=%d",
                            self.camera_id, self.frames_dropped)

    async def _pump_stdin(self) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        try:
            while True:
                body = await self._queue.get()
                self.proc.stdin.write(body)
                await self.proc.stdin.drain()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a dead ffmpeg must not kill the bridge
            log.exception("[%s] passthrough stdin failed", self.camera_id)

    async def _drain_stderr(self) -> None:
        """ffmpeg's complaints are the only diagnosis we get when a
        stream will not start; losing them to a full pipe would also
        block ffmpeg itself."""
        if not self.proc or not self.proc.stderr:
            return
        try:
            async for line in self.proc.stderr:
                text = line.decode(errors="replace").strip()
                if text:
                    log.warning("[%s] ffmpeg: %s", self.camera_id, text)
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict:
        return {
            "running": self.proc is not None and self.proc.returncode is None,
            "started_on_keyframe": self.started,
            "enc": self.enc,
            "transcode": self.transcode,
            "frames_in": self.frames_in,
            "frames_dropped": self.frames_dropped,
            "queued": self._queue.qsize(),
        }
