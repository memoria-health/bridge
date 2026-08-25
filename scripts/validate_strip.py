#!/usr/bin/env python3
"""Does the stripped page still deliver frames, and what does it cost?

Supersedes scripts/nalu_spike.py, which asked whether `frameData` was a
usable encoded stream. It is (measured 2026-08-20: H.265 Annex-B,
VPS/SPS in-band on keyframes), so that question is closed and this asks
the next one.

The open risk in the strip is that `Player.init` is never called. That
is what fills play.js's `playerList`, and skipping it is what removes
the WASM decoder and the canvas — but `Player.OpenStream` and the
decode path both index that list, so the whole thing rests on
`ConnectApi.onrecvframeex` firing upstream in the connector regardless.
Reasoned from the vendor source; unproven until a camera says so.

Runs the REAL runtime page under the REAL headless Chromium, and the
REAL WarnerSource decoding what it forwards. Deliberately no LiveKit and
no backend: this answers "do frames arrive and decode", not "does the
whole product work", and it needs no prod credentials and writes
nothing.

    python3 scripts/validate_strip.py --seconds 30

Prints frames in, frames decoded, and the browser's RSS — the number the
per-camera server cost rests on.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import web

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from vendors.warner.source import WarnerSource  # noqa: E402

SDK_DIR = HERE / "vendors" / "warner" / "sdk"
STATIC_DIR = HERE / "vendors" / "warner" / "static"
CAMERA_ID = "validate"
PORT = 8097

state = {"frames_in": 0, "audio_in": 0, "keyframe": False, "reports": []}


def rss_breakdown(pids: list[int]) -> dict[str, int]:
    """RSS per process KIND, in KB.

    The total for one camera is NOT the per-camera cost, and reporting
    it as such would overstate the fleet by a lot. Chromium's browser,
    GPU and network processes are ONE set however many cameras run;
    only the renderer scales per page. Server capacity depends on the
    marginal number, so the two are kept apart here.
    """
    if not pids:
        return {}
    out = subprocess.run(
        ["ps", "-o", "rss=,time=,command=", *[str(p) for p in pids]],
        capture_output=True, text=True).stdout
    totals: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        kb, cpu_time, command = int(parts[0]), parts[1], parts[2]
        if "--type=renderer" in command:
            kind = "chromium_renderer_PER_CAMERA"
        elif "--type=" in command:
            kind = "chromium_helper_shared"
        elif "Chromium" in command or "chrome" in command.lower():
            kind = "chromium_browser_shared"
        elif "ffmpeg" in command:
            kind = "ffmpeg_PER_CAMERA"
        elif "node" in command:
            kind = "playwright_driver_shared"
        else:
            kind = "other"
        totals[kind] = totals.get(kind, 0) + kb
        # SUMMED, like RSS. Taking the max counted only the busiest of
        # the several processes that share a kind — chromium_helper
        # covers the GPU, network and utility processes — so shared CPU
        # was understated, and the two fields meant different things.
        totals["cpu_s:" + kind] = (
            totals.get("cpu_s:" + kind, 0) + int(_cpu_seconds(cpu_time) * 100))
    return totals


def _cpu_seconds(value: str) -> float:
    """`ps -o time=` is MM:SS, and HH:MM:SS once a process passes an
    hour — which every soak does. The two-field parse silently returned
    0.0 for exactly the runs that matter most."""
    parts = value.strip().split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return 0.0
    seconds = 0.0
    for n in numbers:
        seconds = seconds * 60 + n
    return seconds


def descendants(pid: int, depth: int = 0) -> list[int]:
    """Playwright exposes no PID for the browser, and Chromium is a tree
    of helper processes anyway — renderer, GPU, network — so the only
    honest number is the whole subtree under us."""
    if depth > 6:
        return []
    out = subprocess.run(["pgrep", "-P", str(pid)],
                         capture_output=True, text=True).stdout.split()
    kids = [int(p) for p in out if p.strip().isdigit()]
    return kids + [g for k in kids for g in descendants(k, depth + 1)]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--device-key", default=os.environ.get("WARNER_DEVICE_KEY", ""))
    # Deliberately env-only: a password on the command line is visible
    # in `ps` and lands in shell history.
    ap.add_argument("--camera-user", default=os.environ.get("WARNER_CAMERA_USER", "admin"))
    ap.add_argument("--channel", type=int, default=0)
    # 1 = MAIN. Stream 0 is the 800x448 sub feed. The fourth place this
    # default was wrong: 81eda76 fixed the admin form and the example
    # config, 72f4fcc fixed three Python fallbacks, and this harness —
    # added by 72f4fcc itself — still claimed main-stream dimensions
    # while asking for the sub stream.
    ap.add_argument("--stream-id", type=int, default=1)
    ap.add_argument("--width", type=int, default=2304)
    ap.add_argument("--height", type=int, default=1296)
    args = ap.parse_args()
    args.camera_password = os.environ.get("WARNER_CAMERA_PASSWORD", "")
    if not args.device_key:
        print("need --device-key productId:deviceId:deviceSecret "
              "(or WARNER_DEVICE_KEY)", file=sys.stderr)
        return 2

    source = WarnerSource(CAMERA_ID, width=args.width, height=args.height)
    await source.start(want_audio=False)

    async def config(_request):
        return web.json_response({
            "camera_id": CAMERA_ID,
            "device_key": args.device_key,
            "device_id": args.device_key.split(":")[1],
            "camera_user": args.camera_user,
            "camera_password": args.camera_password,
            "channel": args.channel, "stream_id": args.stream_id,
            "livekit_url": "", "capture_width": args.width,
            "capture_height": args.height,
        })

    async def status(request):
        body = await request.json()
        state["reports"].append(body.get("state"))
        detail = str(body.get("detail", ""))
        if body.get("state") == "frame_heartbeat" and "x" in detail:
            # The camera's OWN size, which the page reports and this used
            # to discard. Without comparing it, asking for the sub stream
            # still prints PASS while the RSS and CPU numbers describe an
            # 800x448 workload presented as the 3MP per-camera cost.
            state["reported_size"] = detail.split()[0]
        print(f"  page: {body.get('state')} {body.get('detail', '')}")
        return web.json_response({"ok": True})

    async def frames(request):
        socket = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024)
        await socket.prepare(request)
        print("  frame socket open")
        async for message in socket:
            if message.type != web.WSMsgType.BINARY:
                continue
            if not message.data:
                continue
            kind = message.data[0]
            if kind == 0:
                state["audio_in"] += 1
            else:
                state["frames_in"] += 1
                if kind == 1:
                    state["keyframe"] = True
            source.offer(message.data)
        return socket

    async def runtime_page(_request):
        return web.FileResponse(STATIC_DIR / "runtime.html")

    app = web.Application()
    app.router.add_get("/runtime", runtime_page)
    app.router.add_get(f"/api/runtime/{CAMERA_ID}/config", config)
    app.router.add_post(f"/api/runtime/{CAMERA_ID}/status", status)
    app.router.add_get(f"/api/runtime/{CAMERA_ID}/frames", frames)
    app.router.add_static("/static", STATIC_DIR)
    app.router.add_static("/sdk", SDK_DIR)
    app.router.add_static("/", SDK_DIR)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    from playwright.async_api import async_playwright
    decoded = 0

    async def count_decoded():
        nonlocal decoded
        async for _frame in source.video_frames():
            decoded += 1

    counter = asyncio.create_task(count_decoded())
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required",
                  "--disable-dev-shm-usage", "--no-sandbox"])
        context = await browser.new_context(viewport={"width": 320, "height": 240})
        page = await context.new_page()
        page.on("console", lambda m: print(f"  browser {m.type}: {m.text}"))
        page.on("pageerror", lambda e: print(f"  browser ERROR: {e}"))
        await page.goto(f"http://127.0.0.1:{PORT}/runtime?camera_id={CAMERA_ID}",
                        wait_until="domcontentloaded")
        started = time.time()
        peak: dict[str, int] = {}
        while time.time() - started < args.seconds:
            await asyncio.sleep(2)
            for kind, kb in rss_breakdown(descendants(os.getpid())).items():
                peak[kind] = max(peak.get(kind, 0), kb)
        await context.close()
        await browser.close()

    counter.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await counter
    await source.stop()
    await runner.cleanup()

    fps = state["frames_in"] / args.seconds
    print(json.dumps({
        "seconds": args.seconds,
        "encoded_frames_in": state["frames_in"],
        "audio_frames_in": state["audio_in"],
        "saw_keyframe": state["keyframe"],
        "decoded_frames_out": decoded,
        "encoded_fps": round(fps, 2),
        "rss_mb": {k: round(v / 1024, 1)
                   for k, v in sorted(peak.items()) if not k.startswith("cpu_s:")},
        "cpu_cores": {k[6:]: round(v / 100 / args.seconds, 2)
                      for k, v in sorted(peak.items()) if k.startswith("cpu_s:")},
        "marginal_per_camera_mb": round(sum(
            v for k, v in peak.items()
            if k.endswith("PER_CAMERA") and not k.startswith("cpu_s:")) / 1024, 1),
        "shared_once_per_box_mb": round(sum(
            v for k, v in peak.items()
            if k.endswith("shared") and not k.startswith("cpu_s:")) / 1024, 1),
        "marginal_per_camera_cores": round(sum(
            v for k, v in peak.items()
            if k.startswith("cpu_s:") and k.endswith("PER_CAMERA"))
            / 100 / args.seconds, 2),
        "camera_reported_size": state.get("reported_size"),
        "page_states": state["reports"],
    }, indent=2))
    # The whole point: frames must arrive WITHOUT Player.init, and must
    # survive the decoder.
    wanted_size = f"{args.width}x{args.height}"
    got_size = state.get("reported_size")
    size_ok = got_size in (None, wanted_size)
    ok = state["frames_in"] > 0 and decoded > 0 and size_ok
    if not size_ok:
        print(f"FAIL: camera is sending {got_size}, not the {wanted_size} "
              f"these numbers claim to describe (wrong --stream-id?)")
    elif not ok:
        print("FAIL: no frames arrived or none decoded")
    else:
        print("PASS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
