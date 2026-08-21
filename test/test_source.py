"""WarnerSource and its ffmpeg lanes, which had no tests at all.

Every bug pinned here was found by review, not by a failing test, and the
worst of them — a decoder that dies and is never restarted — is invisible
from outside: the page keeps reporting frames, the health light stays
green, and the room receives a frozen picture.

The fake process below is deliberately STRICTER than `asyncio`'s: `alive`
is decided by `returncode`, exactly as the real one decides it, so a fake
that merely remembered "I was started" cannot hide the corpse bug.
"""
from __future__ import annotations

import asyncio

import pytest

from vendors.warner import source as src
from vendors.warner.source import WarnerSource, demuxer_for, stream_mismatch


class FakeStdin:
    def __init__(self, proc):
        self.proc = proc
        self.written: list[bytes] = []

    def is_closing(self) -> bool:
        return self.proc.returncode is not None

    def close(self) -> None:
        pass

    def write(self, body: bytes) -> None:
        if self.proc.returncode is not None:
            raise BrokenPipeError("ffmpeg is gone")
        self.written.append(body)

    async def drain(self) -> None:
        return None


class FakeStdout:
    """Hands out whole frames, then blocks — which is what a live
    decoder's stdout does between frames, and the reason a reader that
    only checks liveness on entry never notices a death."""

    def __init__(self, frames: list[bytes]):
        self._frames = list(frames)

    async def readexactly(self, n: int) -> bytes:
        if not self._frames:
            await asyncio.sleep(3600)
        return self._frames.pop(0)


class FakeProc:
    """One ffmpeg. `returncode` is the whole point: a crashed process
    keeps its object, so anything guarding on `proc is not None` sees a
    live decoder that is not there."""

    spawned: list["FakeProc"] = []

    def __init__(self, argv: list[str], frames: list[bytes] | None = None,
                 kwargs: dict | None = None):
        self.argv = list(argv)
        self.kwargs = dict(kwargs or {})
        self.returncode: int | None = None
        self.stdin = FakeStdin(self)
        self.stdout = FakeStdout(frames or [])
        self.stderr = None
        self.terminated = False
        FakeProc.spawned.append(self)

    def die(self, code: int = 1) -> None:
        self.returncode = code

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


@pytest.fixture
def spawns(monkeypatch):
    """Every ffmpeg the code tried to start, in order."""
    FakeProc.spawned.clear()
    frames: dict[int, list[bytes]] = {}

    async def fake_exec(*argv, **kwargs):
        return FakeProc(list(argv), frames.pop(len(FakeProc.spawned), None),
                        kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(src.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(src, "RESTART_BACKOFF_SECONDS", 0.0)
    FakeProc.frames_for = frames
    return FakeProc.spawned


# ── the decode command ────────────────────────────────────────────
def test_output_size_is_pinned_to_the_configured_frame():
    """`video_frames` reads fixed width*height*3/2 chunks. If ffmpeg's
    output size is ever allowed to follow the camera instead, the reader
    desyncs by the remainder and every later frame is garbage — silently,
    with no error anywhere. The pin is the only thing preventing it."""
    argv = src.video_argv("H265", 15, 2304, 1296)
    assert "-s" in argv
    assert argv[argv.index("-s") + 1] == "2304x1296"
    # The pixel format is load-bearing for exactly the same reason: it
    # is what fixes the 3/2 bytes per pixel the reader assumes.
    assert argv[argv.index("-pix_fmt") + 1] == "yuv420p"
    assert argv[-1] == "-"


def test_the_demuxer_follows_the_camera_codec():
    assert src.video_argv("H265", 15, 640, 360)[
        src.video_argv("H265", 15, 640, 360).index("-f") + 1] == "hevc"
    assert src.video_argv("H264", 15, 640, 360)[
        src.video_argv("H264", 15, 640, 360).index("-f") + 1] == "h264"


@pytest.mark.parametrize("spelling", ["H265", "h265", "H.265", "HEVC", "hevc"])
def test_every_spelling_of_h265_reaches_the_hevc_demuxer(spelling):
    """The SDK, the inventory row and the page heartbeat disagree on
    spelling. One that fell through to h264 would decode zero frames."""
    assert demuxer_for(spelling) == "hevc"


# ── the pipe limit, which is what ffmpeg's memory rests on ────────
async def test_the_pipe_is_not_throttled_part_way_through_a_frame(spawns):
    """Worth a test because it is invisible and expensive: asyncio's
    default 64KB limit makes `feed_data` pause the pipe every 128KB while
    `readexactly` is waiting for a whole 4.5MB frame — measured at 20,117
    pause/resume cycles per 300 frames. Each pause stops draining, ffmpeg
    blocks mid-write, its queues back up, and AVBufferPool keeps the
    high-water mark forever. Measured 239MB -> 187MB per camera over ten
    paired runs, and 232MB -> 206MB on the live harness over eleven.

    The limit must therefore be at least one frame; anything smaller
    silently reintroduces the cost.
    """
    source = WarnerSource("cam", width=2304, height=1296)
    await source.start(want_audio=False)
    assert spawns[0].kwargs["limit"] >= source.frame_size


async def test_the_reader_and_the_pipe_limit_use_the_same_frame_size(spawns):
    """They were computed in two places. Two expressions for the size of
    a frame is exactly the duplication that desyncs this reader."""
    source = WarnerSource("cam", width=2304, height=1296)
    await source.start(want_audio=False)
    assert source.frame_size == 2304 * 1296 * 3 // 2
    assert source.video.read_size == source.frame_size


async def test_the_audio_lane_keeps_a_sane_limit(spawns):
    """A 20ms PCM chunk is 1920 bytes, far under the default, so audio
    never had this problem and must not get a 4.5MB buffer for it."""
    source = WarnerSource("cam", width=2304, height=1296)
    await source.start(want_audio=True)
    audio = [p for p in spawns if "aac" in p.argv][0]
    assert audio.kwargs["limit"] == 64 * 1024


# ── finding 1: a dead decoder must come back ──────────────────────
async def test_a_crashed_decoder_is_restarted(spawns):
    """The failure: `start()` guarded on `self.proc is not None`, and a
    crashed ffmpeg leaves that object in place with a returncode. Every
    later start() returned immediately, and nothing above ever calls it
    twice anyway — CameraPublisher starts the source once and its
    `_capturing` flag stays true for the life of the connection. So one
    malformed stream froze the camera until it left the roster."""
    source = WarnerSource("cam", width=2, height=2)
    await source.start(want_audio=False)
    assert len(spawns) == 1

    spawns[0].die()
    assert await source.video.ensure_running() is True
    assert len(spawns) == 2, "a dead lane must get a new ffmpeg"
    assert source.video.alive
    assert source.video.restarts == 1


async def test_the_reader_itself_revives_the_decoder(spawns):
    """The restart has to happen in the loop that NOTICES the death,
    because there is no supervisor above it. This is the docstring claim
    that used to be false."""
    source = WarnerSource("cam", width=2, height=2)
    await source.start(want_audio=False)
    frames = source.video_frames()
    spawns[0].die()

    task = asyncio.create_task(frames.__anext__())
    for _ in range(20):
        await asyncio.sleep(0)
        if len(spawns) > 1:
            break
    task.cancel()
    assert len(spawns) == 2, "reading past a dead decoder must restart it"


async def test_a_restart_re_arms_the_keyframe_gate(spawns):
    """A fresh decoder handed a P-frame emits garbage. `started` used to
    survive the restart, so the gate that exists to prevent exactly that
    was already open."""
    source = WarnerSource("cam", width=2, height=2)
    await source.start(want_audio=False)
    source.offer(bytes([src.FRAME_KEY]) + b"idr")
    assert source.started is True

    spawns[0].die()
    frames = source.video_frames()
    task = asyncio.create_task(frames.__anext__())
    for _ in range(20):
        await asyncio.sleep(0)
        if len(spawns) > 1:
            break
    task.cancel()

    assert source.started is False
    # And a delta frame arriving now is refused, not fed to the new one.
    source.offer(bytes([src.FRAME_DELTA]) + b"pframe")
    assert b"pframe" not in spawns[1].stdin.written


async def test_a_deliberately_stopped_lane_is_not_resurrected(spawns):
    """The reader loops on regardless of whether the camera is meant to
    be publishing. Reviving on liveness alone would undo every stop and
    leave an ffmpeg per disconnected camera."""
    source = WarnerSource("cam", width=2, height=2)
    await source.start(want_audio=False)
    await source.stop()
    before = len(spawns)
    assert await source.video.ensure_running() is False
    assert len(spawns) == before


async def test_a_restart_does_not_feed_the_new_decoder_stale_nalus(spawns):
    """`stop()` left up to MAX_QUEUED NALUs from before the break in the
    queue. The pump drains them into the replacement ffmpeg ahead of the
    keyframe gate, which only inspects what arrives after the restart."""
    source = WarnerSource("cam", width=2, height=2)
    await source.start(want_audio=False)
    source.offer(bytes([src.FRAME_KEY]) + b"idr")
    for _ in range(5):
        source.offer(bytes([src.FRAME_DELTA]) + b"stale")
    assert source.video.status()["queued"] > 0

    await source.video.stop()
    assert source.video.status()["queued"] == 0


# ── finding 2: the camera's real stream must be reported ──────────
def test_a_matching_heartbeat_is_not_a_mismatch():
    assert stream_mismatch("2304x1296 H265", "H265", 2304, 1296) == ""


def test_a_sub_stream_is_caught():
    """The sub-stream bug: a 3MP sensor provisioned onto stream 0 caps at
    800x448. The decoder stays pinned to 2304x1296 and the reader keeps
    reading 4.5MB chunks that will never arrive."""
    problem = stream_mismatch("800x448 H265", "H265", 2304, 1296)
    assert "800x448" in problem and "2304x1296" in problem


def test_a_codec_the_decoder_cannot_read_is_caught():
    """An H.264 unit against the hevc demuxer decodes zero frames and
    `video_frames` blocks on readexactly forever, behind a green light."""
    assert "H264" in stream_mismatch("2304x1296 H264", "H265", 2304, 1296)


def test_a_non_heartbeat_detail_is_not_misread_as_a_mismatch():
    assert stream_mismatch("camera unreachable", "H265", 2304, 1296) == ""
    assert stream_mismatch("", "H265", 2304, 1296) == ""


def test_the_mismatch_is_reported_once_not_every_five_seconds(caplog):
    source = WarnerSource("cam", width=2304, height=1296)
    with caplog.at_level("ERROR"):
        for _ in range(4):
            assert source.check_stream("800x448 H265")
    assert len(caplog.records) == 1, "a line per heartbeat would bury it"


def test_check_stream_does_not_re_pin_the_decoder():
    """Following the camera would trade a loud failure for a silent one:
    the reader's chunk size comes from these numbers."""
    source = WarnerSource("cam", width=2304, height=1296)
    source.check_stream("800x448 H265")
    assert (source.width, source.height) == (2304, 1296)
    assert source.video.argv[source.video.argv.index("-s") + 1] == "2304x1296"


# ── the keyframe gate ─────────────────────────────────────────────
async def test_decoding_does_not_begin_mid_gop(spawns):
    source = WarnerSource("cam", width=2, height=2)
    await source.start(want_audio=False)
    source.offer(bytes([src.FRAME_DELTA]) + b"pframe")
    assert source.video.status()["queued"] == 0
    source.offer(bytes([src.FRAME_KEY]) + b"idr")
    assert source.video.status()["queued"] == 1


async def test_a_page_recreation_re_arms_the_gate(spawns):
    """The stall watchdog recreates the page, not the source, so the gate
    has to be re-armed when the frame socket closes."""
    source = WarnerSource("cam", width=2, height=2)
    await source.start(want_audio=False)
    source.offer(bytes([src.FRAME_KEY]) + b"idr")
    source.require_keyframe()
    assert source.started is False


# ── finding 3 ─────────────────────────────────────────────────────
def test_a_lane_does_not_pretend_to_serve_audio_frames():
    """`_Lane.audio_frames` referenced `self.audio`, which a lane does not
    have, so any call raised AttributeError. It sat directly above
    `status()` looking perfectly callable."""
    assert not hasattr(src._Lane, "audio_frames")
    assert hasattr(WarnerSource, "audio_frames")
