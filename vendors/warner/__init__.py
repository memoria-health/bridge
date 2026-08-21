#!/usr/bin/env python3
"""Warner / Trueview cameras.

Everything brand-specific is here: the `productId:deviceId:deviceSecret`
identity, the H5 SDK assets, and the headless page that runs its P2P
transport. `core/` knows none of it.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from core import store
from core.auth import require_local, require_loopback
from core import livekit_adapters
from core.publisher import CameraPublisher
from core.util import log
from vendors.base import EntityHealth, Publisher, Vendor
from vendors.warner.chromium import CAPTURE_HEIGHT, CAPTURE_WIDTH, ChromiumSupervisor
from vendors.warner.source import WarnerSource

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
SDK_DIR = Path(os.environ.get("WARNER_SDK_DIR", HERE / "sdk"))


class WarnerVendor(Vendor):
    name = "warner"
    # Declared, not assumed, and declared about the BRIDGE rather than the
    # hardware. The camera reports Product.Capability.TwowayTalk, but
    # nothing here implements talk, so listing it would be the exact lie
    # rule 4 exists to prevent. Add it when the code arrives.
    capabilities = ("privacy_sever", "audio_kill")

    def __init__(self) -> None:
        self.chromium = ChromiumSupervisor()
        self._bridge: Any = None
        # One decoder and one publisher per physical camera. The page
        # only carries bytes; these do the work that used to happen in
        # the browser.
        self.sources: dict[str, WarnerSource] = {}
        self.publishers: dict[str, CameraPublisher] = {}
        self.pumps: dict[str, asyncio.Task] = {}

    # ── identity and inventory ────────────────────────────────────
    def camera_id(self, device_key: str) -> str:
        parts = device_key.split(":")
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                "Warner identity must be productId:deviceId:deviceSecret")
        # The secret is deliberately NOT in the hash: rotating it must not
        # orphan the camera's inventory row and its LiveKit identities.
        stable = f"{parts[0]}:{parts[1]}".encode()
        return "warner_" + hashlib.sha256(stable).hexdigest()[:16]

    def entry_from_provision(
        self, body: dict[str, Any], identities: list[str], name: str,
    ) -> dict[str, Any]:
        # Provisioning returns exactly one camera row per physical unit
        # since 2026-08-19; it used to return one per patient.
        device_key = str(body.get("device_key", "")).strip()
        return {
            "camera_id": self.camera_id(device_key),
            "device_id": device_key.split(":")[1],
            "device_key": device_key,
            "name": name,
            "camera_user": str(body.get("camera_user", "admin")),
            "camera_password": str(body.get("camera_password", "")),
            "channel": int(body.get("channel", 0)),
            # 1 = MAIN. stream 0 is the camera's SUB feed, which caps at
            # 800x448 — measured. 81eda76 moved the admin form and the
            # example config to main but left these server-side
            # fallbacks at 0, so a request omitting the field still
            # provisioned a 3MP sensor onto its small stream.
            "stream_id": int(body.get("stream_id", 1)),
            "livekit_identity": identities[0],
        }

    def public_fields(self, entry: dict[str, Any]) -> dict[str, Any]:
        # device_key and camera_password are in the row and stay there.
        return {
            "camera_id": entry["camera_id"],
            "device_id": entry["device_id"],
            "name": entry.get("name", "Warner camera"),
            "channel": entry.get("channel", 0),
            "stream_id": entry.get("stream_id", 1),
            "livekit_identity": entry.get("livekit_identity"),
        }

    # ── lifecycle ─────────────────────────────────────────────────
    def preflight(self) -> None:
        if not SDK_DIR.joinpath("connector.js").exists():
            raise RuntimeError(f"Warner SDK not found at {SDK_DIR}; set WARNER_SDK_DIR")
        if not SDK_DIR.joinpath("play.js").exists():
            raise RuntimeError("warner sdk/play.js is required")

    async def start(self) -> None:
        await self.chromium.start()

    async def apply(self, plan: dict[str, Publisher | None]) -> None:
        # The page first: it is what produces the bytes everything below
        # consumes, and it is cheap to have running with no subscriber.
        await self.chromium.apply(plan)
        for camera_id, spec in plan.items():
            publisher = self.publishers.get(camera_id)
            if publisher is None:
                if spec is None or not spec.connected:
                    continue
                publisher = self._build_publisher(camera_id)
            try:
                await publisher.apply(spec)
            except Exception as exc:  # noqa: BLE001
                # ISOLATED per camera. An unguarded raise here unwound to
                # the supervisor's log-and-sleep, so every camera later in
                # the plan was skipped for that pass — and since the order
                # is inventory order, the same ones every time. A camera
                # reclassified as a bathroom behind a failing one never
                # had its subscription permissions applied at all.
                log(f"[{camera_id}] reconcile failed: {exc}")
        for camera_id in [c for c in self.publishers if c not in plan]:
            await self._retire(camera_id)

    def _build_publisher(self, camera_id: str) -> CameraPublisher:
        camera = store.find(camera_id) or {}
        source = WarnerSource(
            camera_id,
            width=int(camera.get("capture_width", CAPTURE_WIDTH)),
            height=int(camera.get("capture_height", CAPTURE_HEIGHT)),
        )
        publisher = CameraPublisher(
            camera_id=camera_id,
            source=source,
            livekit_url=self._bridge.settings["livekit_url"],
            token_for=self._bridge.mint_token,
            room_factory=livekit_adapters.room,
            video_source_factory=livekit_adapters.VideoSource,
            audio_source_factory=livekit_adapters.AudioSource,
        )
        self.sources[camera_id] = source
        self.publishers[camera_id] = publisher
        self.pumps[camera_id] = asyncio.gather(
            self._pump(camera_id, publisher.pump, "video"),
            self._pump(camera_id, publisher.pump_audio, "audio"))
        return publisher

    async def _pump(self, camera_id: str, run: Any, what: str) -> None:
        """One media lane into LiveKit, for as long as this camera exists.

        RESTARTS. It used to log once and return, so a single raise ended
        the lane permanently — and for audio that reproduced the exact
        failure the live-lane work was written to prevent: a published
        track carrying no PCM, silent and indistinguishable from working
        audio. A capture_frame against a track disposed a moment earlier
        is enough to trigger it.

        Video and audio are separate tasks on purpose: audio failing must
        not take the picture a caregiver is watching.
        """
        while True:
            try:
                await run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — must not kill the loop
                log(f"[{camera_id}] {what} pump failed, restarting: {exc}")
            # A pump that returns cleanly has run out of source, which
            # happens while the decoder is being restarted. Pause before
            # re-entering so a hard failure cannot spin.
            await asyncio.sleep(1)

    async def _retire(self, camera_id: str) -> None:
        pump = self.pumps.pop(camera_id, None)
        if pump:
            pump.cancel()
        publisher = self.publishers.pop(camera_id, None)
        if publisher:
            await publisher.close()
        self.sources.pop(camera_id, None)

    def health(self) -> dict[str, EntityHealth]:
        """The page's view of the camera, plus the publisher's view of
        LiveKit.

        The page alone is not enough any more. It reports frames arriving
        from the vendor SDK, which proves the camera is alive — but since
        publishing moved to Python, the decode, the frame socket, the
        room and the pump can all be dead behind that green light.
        """
        for camera_id, health in self.chromium.status.items():
            publisher = self.publishers.get(camera_id)
            if publisher is None:
                health.publishing = False
                health.last_publish_at = 0.0
                continue
            # Observed, not planned: the publisher writes `applied` only
            # after the SFU accepted the call.
            health.applied = publisher.applied
            health.publishing = publisher.connection is not None
            health.last_publish_at = publisher.last_published_at
        return self.chromium.status

    def report(self, camera_id: str, body: dict[str, Any]) -> None:
        self.chromium.report(camera_id, body)

    async def close(self) -> None:
        for camera_id in list(self.publishers):
            await self._retire(camera_id)
        await self.chromium.close()

    # ── HTTP ──────────────────────────────────────────────────────
    def static_mounts(self) -> dict[str, Path]:
        # The catch-all at "/" is last on purpose: several Warner bundles
        # resolve workers, WASM and images against the document root
        # (/j-decoder.wasm, /mqtt/mqttWorker.js, /logo_bg.png) rather than
        # against their own script URL.
        return {"/static": STATIC_DIR, "/sdk": SDK_DIR, "/": SDK_DIR}

    def add_routes(self, app: web.Application, bridge: Any) -> None:
        self._bridge = bridge
        app.router.add_get("/runtime", self._runtime_page)
        app.router.add_get("/api/runtime/{camera_id}/frames", self._frames_socket)
        app.router.add_get("/api/runtime/{camera_id}/config", self._runtime_config)
        app.router.add_get("/api/debug/{camera_id}/model", self._device_model)

    async def _runtime_page(self, request: web.Request) -> web.FileResponse:
        require_local(request)
        return web.FileResponse(STATIC_DIR / "runtime.html")

    async def _runtime_config(self, request: web.Request) -> web.Response:
        """The highest-value response on the bridge: it carries the device
        secret and the camera's admin password, because the vendor SDK
        genuinely needs both. Loopback only, even with a valid API key."""
        require_loopback(request)
        camera = store.find(request.match_info["camera_id"])
        if not camera:
            raise web.HTTPNotFound(text="camera not found")
        return web.json_response({
            "camera_id": camera["camera_id"],
            "device_key": camera["device_key"],
            "device_id": camera["device_id"],
            "camera_user": camera.get("camera_user", "admin"),
            "camera_password": camera.get("camera_password", ""),
            "channel": camera.get("channel", 0),
            "stream_id": camera.get("stream_id", 1),
            "livekit_url": self._bridge.settings["livekit_url"],
            # Starting size only; the runtime adopts the camera's real
            # frame size on its first frame. It must not be 720p on a 3MP
            # sensor, which is what the markup used to hardcode.
            "capture_width": int(camera.get("capture_width", CAPTURE_WIDTH)),
            "capture_height": int(camera.get("capture_height", CAPTURE_HEIGHT)),
        })

    async def _frames_socket(self, request: web.Request) -> web.WebSocketResponse:
        """Encoded frames in from the page, straight into the decoder.

        Loopback only: the sender is our own headless page, and this is
        the one route that injects video into a patient's room.
        """
        require_loopback(request)
        camera_id = request.match_info["camera_id"]
        source = self.sources.get(camera_id)
        if source is None:
            raise web.HTTPNotFound(text="camera is not publishing")
        # Bounded, so a burst cannot become unbounded bridge memory. A
        # 3MP keyframe measured ~20KB, so this is generous by 100x and
        # still a ceiling.
        socket = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024)
        await socket.prepare(request)
        log(f"[{camera_id}] frame socket open")
        try:
            async for message in socket:
                if message.type == web.WSMsgType.BINARY:
                    source.offer(message.data)
        finally:
            log(f"[{camera_id}] frame socket closed")
        return socket

    async def _page_for(self, request: web.Request) -> Any:
        require_local(request)
        page = self.chromium.pages.get(request.match_info["camera_id"])
        if not page:
            raise web.HTTPNotFound(text="camera worker is not active")
        return page

    async def _device_model(self, request: web.Request) -> web.Response:
        """What this camera will let us change. The vendor keeps no
        property list in the SDK; it comes per product from their cloud."""
        page = await self._page_for(request)
        camera = store.find(request.match_info["camera_id"])
        device_id = str((camera or {}).get("device_id", ""))
        if not device_id:
            raise web.HTTPNotFound(text="camera has no device id")
        model = await page.evaluate(
            """async (deviceId) => {
                const res = await ConnectApi.getDeviceModel(deviceId);
                const fns = res?.data?.data?.productFun || [];
                return fns.map(f => ({
                    code: f.funCode, name: f.funName,
                    spec: f.funContent?.typeSpec || null }));
            }""", device_id)
        return web.json_response(
            {"camera_id": request.match_info["camera_id"], "model": model})
