#!/usr/bin/env python3
"""What does publishing to LiveKit actually cost per camera?

The last unknown in the per-camera number. `validate_strip.py` measures
the transport and the decode against a real camera but deliberately does
not publish, so the encode — expected to be the largest CPU item — was
still an estimate carried over from an offline libx264 benchmark.

Runs against a LOCAL LiveKit in dev mode, so it touches nothing in
production and needs no production credentials. `devkey`/`secret` are
LiveKit's own documented sandbox values.

    docker run -d --rm --name lk -p 7880:7880 -p 7881:7881 \\
      -e LIVEKIT_KEYS="devkey: secret" livekit/livekit-server --dev
    python3 scripts/measure_encode.py --seconds 30

Frames are synthetic but MOVING. A static picture would let the encoder
skip almost all of its work and understate the cost by an order of
magnitude — the same reason a still room costs ~190 Kbps on the camera
itself.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from core.publisher import CameraPublisher  # noqa: E402
from core import livekit_adapters  # noqa: E402
from vendors.base import Publisher  # noqa: E402


class MovingSource:
    """A frame source the encoder cannot cheat on.

    Implements the same contract WarnerSource does, so this measures the
    real publisher rather than a special case of it.
    """

    def __init__(self, width: int, height: int, fps: int, seconds: float):
        self.width, self.height, self.fps = width, height, fps
        self._frames = int(fps * seconds)
        self.started = False

    async def start(self, want_audio: bool = True) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def set_audio(self, enabled: bool) -> None:
        pass

    async def video_frames(self):
        size = self.width * self.height * 3 // 2
        luma = self.width * self.height
        period = 1.0 / self.fps
        base = bytearray(size)
        # Mid-grey chroma; a black frame would compress to almost nothing.
        for i in range(luma, size):
            base[i] = 128
        next_at = time.monotonic()
        for n in range(self._frames):
            # Shift a bright band down the frame every tick, so every
            # macroblock row changes over the run and the encoder has
            # genuine residual to code.
            frame = bytearray(base)
            row = (n * 37) % self.height
            start = row * self.width
            frame[start:start + self.width * 8] = b"\xe0" * min(
                self.width * 8, luma - start)
            for i in range(0, luma, 991):          # sparse noise, cheap to make
                frame[i] = (n * 7 + i) & 0xFF
            next_at += period
            delay = next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            yield bytes(frame)


def cpu_seconds() -> float:
    me = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (me.ru_utime + me.ru_stime + kids.ru_utime + kids.ru_stime)


def rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--width", type=int, default=2304)
    ap.add_argument("--height", type=int, default=1296)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--url", default=os.environ.get("LK_URL", "ws://127.0.0.1:7880"))
    ap.add_argument("--key", default=os.environ.get("LK_KEY", "devkey"))
    ap.add_argument("--secret", default=os.environ.get("LK_SECRET", "secret"))
    args = ap.parse_args()

    from livekit import api

    def token_for(identity: str, room: str) -> str:
        return (api.AccessToken(args.key, args.secret)
                .with_identity(identity).with_name(identity)
                .with_grants(api.VideoGrants(room_join=True, room=room))
                .to_jwt())

    source = MovingSource(args.width, args.height, args.fps, args.seconds)
    publisher = CameraPublisher(
        camera_id="measure",
        source=source,
        livekit_url=args.url,
        token_for=token_for,
        room_factory=livekit_adapters.room,
        video_source_factory=livekit_adapters.VideoSource,
        audio_source_factory=livekit_adapters.AudioSource,
    )

    await publisher.apply(Publisher(
        identity="cam_measure", room="measure-scratch", connected=True,
        audio_capture_enabled=False, privacy_enabled=False))
    if publisher.connection is None:
        print("could not connect to LiveKit; is the dev server up?",
              file=sys.stderr)
        return 2

    before, wall = cpu_seconds(), time.monotonic()
    await publisher.pump()
    elapsed = time.monotonic() - wall
    cores = (cpu_seconds() - before) / elapsed
    published = publisher.frames_published
    await publisher.close()

    print(json.dumps({
        "resolution": f"{args.width}x{args.height}",
        "target_fps": args.fps,
        "seconds": round(elapsed, 1),
        "frames_published": published,
        "actual_fps": round(published / elapsed, 2),
        "encode_cores_per_camera": round(cores, 2),
        "peak_rss_mb": round(rss_mb(), 1),
    }, indent=2))
    # A run that could not keep up is not a measurement of the encode, it
    # is a measurement of this machine giving up.
    ok = published >= args.fps * elapsed * 0.9
    print("PASS" if ok else "FAIL: could not sustain the frame rate")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
