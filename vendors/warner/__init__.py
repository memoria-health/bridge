#!/usr/bin/env python3
"""Warner / Trueview cameras.

Everything brand-specific is here: the `productId:deviceId:deviceSecret`
identity, the H5 SDK assets, and the headless page that runs its P2P
transport. `core/` knows none of it.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from core import store
from core.auth import require_local, require_loopback
from vendors.base import EntityHealth, Publisher, Vendor
from vendors.warner.chromium import CAPTURE_HEIGHT, CAPTURE_WIDTH, ChromiumSupervisor

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
            "stream_id": int(body.get("stream_id", 0)),
            "livekit_identity": identities[0],
        }

    def public_fields(self, entry: dict[str, Any]) -> dict[str, Any]:
        # device_key and camera_password are in the row and stay there.
        return {
            "camera_id": entry["camera_id"],
            "device_id": entry["device_id"],
            "name": entry.get("name", "Warner camera"),
            "channel": entry.get("channel", 0),
            "stream_id": entry.get("stream_id", 0),
            "livekit_identity": entry.get("livekit_identity"),
        }

    # ── lifecycle ─────────────────────────────────────────────────
    def preflight(self) -> None:
        if not SDK_DIR.joinpath("connector.js").exists():
            raise RuntimeError(f"Warner SDK not found at {SDK_DIR}; set WARNER_SDK_DIR")
        if not SDK_DIR.joinpath("js", "livekit-client.umd.js").exists():
            raise RuntimeError("warner sdk/js/livekit-client.umd.js is required")

    async def start(self) -> None:
        await self.chromium.start()

    async def apply(self, plan: dict[str, Publisher | None]) -> None:
        await self.chromium.apply(plan)

    def health(self) -> dict[str, EntityHealth]:
        return self.chromium.status

    def report(self, camera_id: str, body: dict[str, Any]) -> None:
        self.chromium.report(camera_id, body)

    async def close(self) -> None:
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
        app.router.add_get("/api/runtime/{camera_id}/config", self._runtime_config)
        app.router.add_get("/api/debug/{camera_id}/screenshot", self._screenshot)
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
            "stream_id": camera.get("stream_id", 0),
            "livekit_url": self._bridge.settings["livekit_url"],
            # Starting size only; the runtime adopts the camera's real
            # frame size on its first frame. It must not be 720p on a 3MP
            # sensor, which is what the markup used to hardcode.
            "capture_width": int(camera.get("capture_width", CAPTURE_WIDTH)),
            "capture_height": int(camera.get("capture_height", CAPTURE_HEIGHT)),
        })

    async def _page_for(self, request: web.Request) -> Any:
        require_local(request)
        page = self.chromium.pages.get(request.match_info["camera_id"])
        if not page:
            raise web.HTTPNotFound(text="camera worker is not active")
        return page

    async def _screenshot(self, request: web.Request) -> web.Response:
        page = await self._page_for(request)
        return web.Response(body=await page.screenshot(type="png"),
                            content_type="image/png",
                            headers={"Cache-Control": "no-store"})

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
