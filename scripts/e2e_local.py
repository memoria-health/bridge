#!/usr/bin/env python3
"""The whole bridge, end to end, against a real camera and a real LiveKit.

`validate_strip.py` proves frames arrive and decode. `measure_encode.py`
prices the encode. Neither runs the actual product: the reconcile loop,
the roster contract, the privacy sever, the audio kill and the
disconnect switch are the things a caregiver actually touches, and until
now they existed only in unit tests with fake LiveKit.

This starts `bridge.py` itself as a subprocess, points it at a stub
backend and a LOCAL LiveKit, and drives the flags the app drives:

    camera -> Chromium (P2P only) -> ffmpeg -> rtc.VideoSource -> LiveKit
                     ^                                              |
                  stub roster  <---- report ---- bridge  ----> participant

Nothing touches production. The stub stands in for the backend, whose
own behaviour is covered by 375 integration tests over there.

    docker run -d --rm --name lk -p 7880:7880 -p 7881:7881 \\
      -p 7882:7882/udp -e LIVEKIT_CONFIG="$(cat scripts/livekit-dev.yaml)" \\
      livekit/livekit-server
    WARNER_DEVICE_KEY=... python3 scripts/e2e_local.py
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import ClientSession, web

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

STUB_PORT = 8199
BRIDGE_PORT = 8198
IDENTITY = "cam_e2e"
ROOM = "e2e-scratch"

# What the stub currently wants. The test mutates this and then waits for
# the bridge to catch up, which is exactly how the app behaves.
desired: dict[str, object] = {
    "desired_state": "connected",
    "privacy_enabled": False,
    "audio_capture_enabled": False,
}
reports: list[dict] = []


async def run_stub() -> web.AppRunner:
    async def poll(request: web.Request) -> web.Response:
        body = await request.json()
        reports.append(body)
        return web.json_response({"contract": "fleet-v1", "cameras": [{
            "camera_id": "e2e", "livekit_identity": IDENTITY,
            "livekit_room": ROOM, "online": True, **desired}]})

    async def token(request: web.Request) -> web.Response:
        """The backend mints every token now; the bridge holds no secret."""
        from livekit import api
        body = await request.json()
        return web.json_response({"token": (
            api.AccessToken("devkey", "secret")
            .with_identity(body["identity"]).with_name(body["identity"])
            .with_grants(api.VideoGrants(room_join=True, room=body["room"],
                                         can_publish=True, can_subscribe=True))
            .to_jwt())})

    app = web.Application()
    app.router.add_post("/v1/fleet/poll", poll)
    app.router.add_post("/v1/livekit/token", token)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", STUB_PORT).start()
    return runner


async def join_as(identity: str, url: str):
    """A real subscriber in the room, so the privacy rule can be checked
    at the SFU instead of being taken on trust."""
    from livekit import api, rtc
    room = rtc.Room()
    token = (api.AccessToken("devkey", "secret")
             .with_identity(identity).with_name(identity)
             .with_grants(api.VideoGrants(room_join=True, room=ROOM))
             .to_jwt())
    await room.connect(url, token, rtc.RoomOptions(auto_subscribe=True))
    return room


async def health(session: ClientSession) -> dict:
    try:
        async with session.get(
            f"http://127.0.0.1:{BRIDGE_PORT}/api/health", timeout=5,
        ) as response:
            return await response.json()
    except Exception:  # noqa: BLE001 — the bridge may not be up yet
        return {}


async def wait_for(session: ClientSession, predicate, what: str,
                   timeout: float = 90) -> dict:
    """Poll health until it says what we are waiting for. Never sleeps a
    fixed amount and hopes: a flaky e2e is worse than none."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = await health(session)
        # /api/health serves the same LIST the fleet poll sends, one row
        # per camera, keyed by "id" inside the row.
        entity = next((e for e in (last.get("entities") or [])
                       if e.get("id") == "e2e"), {})
        if predicate(entity):
            return entity
        await asyncio.sleep(1)
    print(f"TIMED OUT waiting for {what}\n{json.dumps(last, indent=2)}")
    raise SystemExit(1)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device-key", default=os.environ.get("WARNER_DEVICE_KEY", ""))
    ap.add_argument("--livekit", default=os.environ.get("LK_URL", "ws://127.0.0.1:7880"))
    ap.add_argument("--stream-id", type=int, default=1)
    args = ap.parse_args()
    if not args.device_key:
        print("need WARNER_DEVICE_KEY", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="bridge-e2e-"))
    (workdir / "cameras.json").write_text(json.dumps({
        "backend_url": f"http://127.0.0.1:{STUB_PORT}",
        "livekit_url": args.livekit,
        "poll_seconds": 2, "stream_mode": "policy",
    }))
    (workdir / "inventory.json").write_text(json.dumps({"version": 1, "cameras": [{
        "camera_id": "e2e",
        "device_id": args.device_key.split(":")[1],
        "device_key": args.device_key,
        "name": "e2e camera", "camera_user": "admin", "camera_password":
            os.environ.get("WARNER_CAMERA_PASSWORD", ""),
        "channel": 0, "stream_id": args.stream_id,
        "livekit_identity": IDENTITY,
    }]}))

    env = {**os.environ,
           "BRIDGE_GATEWAY_CONFIG": str(workdir / "cameras.json"),
           "BRIDGE_INVENTORY": str(workdir / "inventory.json"),
           "BRIDGE_IDENTITY": str(workdir / "identity.json"),
           "BRIDGE_PORT": str(BRIDGE_PORT),
           "BRIDGE_ORIGIN": f"http://127.0.0.1:{BRIDGE_PORT}",
           "PLAYWRIGHT_BROWSERS_PATH": str(HERE / "browsers")}

    stub = await run_stub()
    bridge = await asyncio.create_subprocess_exec(
        sys.executable, "-u", str(HERE / "bridge.py"),
        cwd=str(HERE), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    async def echo():
        assert bridge.stdout is not None
        async for line in bridge.stdout:
            print("  bridge:", line.decode(errors="replace").rstrip())

    echoing = asyncio.create_task(echo())
    results: dict[str, object] = {}
    try:
        async with ClientSession() as session:
            # 1. it publishes, and health only says so once frames have
            #    actually reached LiveKit.
            entity = await wait_for(
                session, lambda e: e.get("healthy") is True, "a healthy camera")
            results["publishes"] = True
            results["applied_on_connect"] = entity.get("applied")
            baseline_restarts = entity.get("restarts", 0)

            # 2. privacy, applied live rather than at the next reconnect.
            #
            # A detector joins FIRST, because the allow-list is built
            # from the participants actually in the room. Without one the
            # call succeeds against an empty list, which proves the call
            # shape and nothing about the rule — and the rule is the
            # whole point: a private camera must stay visible to the AI
            # while being invisible to caregivers.
            detector = await join_as("detector_worker_e2e", args.livekit)
            caregiver = await join_as("caregiver_e2e", args.livekit)
            desired["privacy_enabled"] = True
            entity = await wait_for(
                session,
                lambda e: (e.get("applied") or {}).get("privacy_enabled") is True,
                "privacy to take effect", timeout=30)
            results["privacy_applied_live"] = True
            results["still_healthy_under_privacy"] = entity.get("healthy")
            # The SFU decides, so ask it rather than ourselves: only the
            # AI should end up subscribed to the camera's track.
            await asyncio.sleep(4)
            results["ai_can_see_private_camera"] = any(
                pub.track is not None
                for p in detector.remote_participants.values()
                if p.identity == IDENTITY
                for pub in p.track_publications.values())
            cg_state = [
                {"sid": pub.sid, "subscribed": pub.subscribed,
                 "has_track": pub.track is not None}
                for p in caregiver.remote_participants.values()
                if p.identity == IDENTITY
                for pub in p.track_publications.values()]
            ai_state = [
                {"sid": pub.sid, "subscribed": pub.subscribed,
                 "has_track": pub.track is not None}
                for p in detector.remote_participants.values()
                if p.identity == IDENTITY
                for pub in p.track_publications.values()]
            results["_caregiver_publications"] = cg_state
            results["_detector_publications"] = ai_state
            # NOT asserted: that a caregiver ALREADY subscribed loses the
            # track. Publisher-side permissions gate new subscriptions and
            # do not revoke existing ones, and the bridge deliberately does
            # not republish to force it — that would drop the detector and
            # the identity service too, blinding the AI to enforce a rule
            # about human viewing. Cutting an existing caregiver is the
            # backend's job (services/livekit_privacy.sever_caregiver_media),
            # which this stub does not implement.
            results["_caregiver_already_subscribed_keeps_track"] = any(
                x["has_track"] for x in cg_state)
            # Does the rule hold for someone who arrives AFTER the
            # camera was marked private? That distinction decides whether
            # this is a revocation gap or a total failure.
            late = await join_as("caregiver_late", args.livekit)
            await asyncio.sleep(5)
            results["_late_caregiver_publications"] = [
                {"sid": pub.sid, "subscribed": pub.subscribed,
                 "has_track": pub.track is not None}
                for p in late.remote_participants.values()
                if p.identity == IDENTITY
                for pub in p.track_publications.values()]
            results["late_caregiver_blocked"] = not any(
                x["has_track"] for x in results["_late_caregiver_publications"])
            await late.disconnect()
            await detector.disconnect()
            await caregiver.disconnect()

            # 3. the audio kill, and then permitting it again.
            desired["audio_capture_enabled"] = True
            await wait_for(
                session,
                lambda e: (e.get("applied") or {}).get(
                    "audio_capture_enabled") is True,
                "audio to be published", timeout=30)
            desired["audio_capture_enabled"] = False
            await wait_for(
                session,
                lambda e: (e.get("applied") or {}).get(
                    "audio_capture_enabled") is False,
                "the audio kill to take effect", timeout=30)
            results["audio_toggles_both_ways"] = True
            # The camera must stay UP across the toggle. The audio kill
            # failing used to tear the publisher down and republish it —
            # briefly satisfying the applied assertions above while
            # actually dropping the picture and re-opening audio at a
            # site that had just forbidden it.
            restarts_now = (await wait_for(
                session, lambda e: True, "a health read", timeout=10)
            ).get("restarts", 0)
            results["no_teardown_during_audio_toggle"] = (
                restarts_now == baseline_restarts)

            # 4. the switch a caregiver presses.
            desired["desired_state"] = "disconnected"
            await wait_for(
                session,
                lambda e: e.get("healthy") is not True,
                "the camera to stop publishing", timeout=45)
            results["disconnect_stops_it"] = True

            # 5. and the report the backend receives.
            last = reports[-1]
            results["report_contract"] = last.get("contract")
            results["report_names_instance"] = bool(
                last.get("service", {}).get("instance_id"))
            results["report_is_per_camera"] = [
                e.get("id") for e in last.get("entities", [])]
    finally:
        echoing.cancel()
        with contextlib.suppress(Exception):
            bridge.terminate()
            await asyncio.wait_for(bridge.wait(), timeout=15)
        await stub.cleanup()
        shutil.rmtree(workdir, ignore_errors=True)

    print(json.dumps(results, indent=2))
    required = ("publishes", "privacy_applied_live",
                "ai_can_see_private_camera",
                "late_caregiver_blocked",
                "audio_toggles_both_ways",
                "no_teardown_during_audio_toggle", "disconnect_stops_it")
    ok = all(results.get(k) for k in required)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
