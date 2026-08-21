#!/usr/bin/env python3
"""Warner frame source — a drop-in for the gateway's RTSP puller.

WHY THIS SHAPE

`nemara-gateway/publisher.py` already solves every hard part of getting
a camera into LiveKit correctly: desired_state per publisher, the
facilities audio kill, the bathroom/bedroom privacy sever re-asserted on
every poll, a frame watchdog for stalls that never EOF, LiveKit
reconnect, and fan-out of one physical stream to many patient rooms. It
publishes from Python through `rtc.VideoSource` as an ordinary `cam_*`
participant — no Ingress, and no new participant-identity plumbing for
the webhooks and `room_reconciler` to trip over.

The Warner bridge reimplemented none of that and got each piece wrong in
a different way. So we do not write a second publisher; we give the
existing one a different way to obtain frames.

    gateway:  ffmpeg(rtsp)                  -> rawvideo -> rtc.VideoSource
    warner:   browser(P2P) -> NALUs -> ffmpeg -> rawvideo -> rtc.VideoSource

The browser stays ONLY because the vendor's cloud/P2P transport and auth
are JS + WASM with no native SDK. It stops being a video pipeline: no
`Player.init` rendering, no canvas, no `captureStream`, no WASM decoder.
It receives P2P packets and hands us the encoded frames the SDK was
already giving it (see ENCODED_PASSTHROUGH.md — measured H.265,
Annex-B, VPS/SPS in-band on keyframes, ~190 Kbps).

WHAT THAT DELETES, per camera: a WASM HEVC decoder and its heap, a 3MP
canvas and its rasterisation, and a browser WebRTC encoder. What
replaces them: one native ffmpeg decode. The encode moves to the LiveKit
SDK, which is where the gateway already does it.

The output contract is deliberately byte-identical to
`build_ffmpeg_cmd`: rawvideo, yuv420p, constant rate on stdout. So the
consumer loop, the stall watchdog and the motion sampling are reused
unchanged rather than re-derived.
"""
from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("warner.source")

# Frame-type byte the runtime page prepends, matching the vendor's own
# constants (PROC_FRAME_TYPE_IFRAME / _PFRAME).
FRAME_KEY = 1
FRAME_DELTA = 2

# Bound the hand-off queue. The identity OOM (2026-08-15) was unbounded
# stream queues; a live view wants the newest picture, so drop the
# oldest rather than grow.
MAX_QUEUED_FRAMES = 120


def build_ffmpeg_cmd(enc: str, fps: int) -> list[str]:
    """Encoded frames on stdin, rawvideo on stdout.

    Mirrors the gateway's RTSP command in everything after the input, so
    the frame reader downstream cannot tell the two sources apart.

    Deliberately NOT `-fflags +nobuffer`. That flag appears on the
    gateway's AUDIO command, and copying it here produced exactly ZERO
    decoded frames from a raw elementary stream — measured, not
    guessed. It is safe on a container with its own timing and wrong on
    a bare Annex-B pipe. `-flags low_delay` is fine and is kept.

    `-fps_mode cfr` rather than the deprecated `-vsync cfr`: the vendor
    hands us a millisecond timestamp per frame but no container timing,
    and the publisher re-stamps presentation time from its own frame
    counter anyway (`ts = self.frames * self.frame_us`), so a constant
    output rate is what the consumer expects.
    """
    fmt = "hevc" if enc.upper().startswith("H265") else "h264"
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-flags", "low_delay",
        "-f", fmt, "-i", "pipe:0",
        "-fps_mode", "cfr", "-r", str(max(1, int(round(fps)))),
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-",
    ]


class WarnerSource:
    """One physical Warner camera: NALUs in, decoded frames out.

    Exposes `.proc` so the caller reads `proc.stdout` exactly as it
    reads the RTSP ffmpeg's stdout today.
    """

    def __init__(self, camera_id: str, enc: str = "H265", fps: int = 15):
        self.camera_id = camera_id
        self.enc = enc
        self.fps = fps
        self.proc: "asyncio.subprocess.Process | None" = None
        self.started = False        # seen a keyframe yet?
        self.frames_in = 0
        self.frames_dropped = 0
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue(
            maxsize=MAX_QUEUED_FRAMES)
        self._pump: "asyncio.Task | None" = None

    async def start(self) -> None:
        if self.proc is not None:
            return
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not on PATH")
        argv = build_ffmpeg_cmd(self.enc, self.fps)
        log.info("[%s] decode start: %s", self.camera_id, " ".join(argv))
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
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

    def offer(self, payload: bytes) -> None:
        """One framed message from the runtime page. Never blocks.

        Refuses to begin mid-GOP: without the parameter sets and an IDR
        the decoder emits garbage, and a caregiver seeing a corrupted
        picture is worse than seeing none. VPS/SPS ride in-band on
        keyframes here, so waiting for one costs at most a GOP.
        """
        if not payload:
            return
        frame_type, body = payload[0], payload[1:]
        if not body:
            return
        if not self.started:
            if frame_type != FRAME_KEY:
                return
            self.started = True
            log.info("[%s] first keyframe — decoding", self.camera_id)
        self.frames_in += 1
        try:
            self._queue.put_nowait(body)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(body)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            self.frames_dropped += 1
            if self.frames_dropped % 100 == 1:
                log.warning("[%s] decode backlog, dropped=%d",
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
        except Exception:  # noqa: BLE001 — a dead decoder must not kill the loop
            log.exception("[%s] decode stdin failed", self.camera_id)

    async def _drain_stderr(self) -> None:
        """ffmpeg's complaints are the only diagnosis when a stream will
        not start, and an unread stderr pipe eventually blocks ffmpeg."""
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
            "fps": self.fps,
            "frames_in": self.frames_in,
            "frames_dropped": self.frames_dropped,
            "queued": self._queue.qsize(),
        }
