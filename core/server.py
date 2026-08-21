#!/usr/bin/env python3
"""The bridge's HTTP surface and the object that holds it together.

Every route here is vendor-neutral. A vendor adds its own through
`Vendor.add_routes`, which is also where anything carrying camera
credentials belongs.
"""
from __future__ import annotations

import asyncio
import time
import urllib.error
from pathlib import Path
from typing import Any

from aiohttp import web
from livekit import api

from core import fleet, store
from core.auth import require_local
from core.config import FRAME_STALL_SECONDS, VERSION
from core.roster import Roster
from core.util import log, post_json
from vendors.base import Vendor


class Bridge:
    def __init__(self, settings: dict[str, Any], vendor: Vendor):
        self.settings = settings
        self.vendor = vendor
        self.roster = Roster(settings, self.fleet_report)

    def fleet_report(self) -> dict[str, Any]:
        """This poll's body: who we are and how every camera we own is
        doing. Built fresh each time — a cached report would let a dead
        camera keep looking alive."""
        return fleet.report_body(
            instance_id=self.settings["instance_id"],
            kind="camera-bridge",
            version=VERSION,
            capabilities=self.vendor.capabilities,
            entities=self.vendor.health(),
            now=time.time(),
            stall_seconds=FRAME_STALL_SECONDS,
        )

    def mint_token(self, identity: str, room: str) -> str:
        """A LiveKit token for one of OUR publishers.

        In-process because the publisher is now in-process; the HTTP
        route below stays for the runtime page and applies the same
        check, that the backend currently wants this publisher.
        """
        return (api.AccessToken(
            self.settings["livekit_api_key"], self.settings["livekit_api_secret"])
            .with_identity(identity).with_name(identity)
            .with_grants(api.VideoGrants(room_join=True, room=room))
            .to_jwt())

    def plan(self) -> dict[str, list]:
        return self.roster.plan(store.cameras())

    async def run_vendor(self) -> None:
        """Reconcile the vendor to the backend's wishes, forever."""
        await self.vendor.start()
        while True:
            try:
                await self.vendor.apply(self.plan())
            except Exception as exc:  # noqa: BLE001 — one bad pass must not end the loop
                log(f"vendor reconcile failed: {exc}")
            await asyncio.sleep(2)

    # ── routes ────────────────────────────────────────────────────
    async def provision(self, request: web.Request) -> web.Response:
        """Claim a physical camera onto a caregiver account.

        Authorized because this is the route that reaches the backend
        with our gateway token and then writes camera credentials to
        disk; it was briefly open, which made an email plus network
        reach enough to provision onto someone's account.
        """
        require_local(request)
        body = await request.json()
        email = str(body.get("email", "")).strip()
        name = str(body.get("name", f"{self.vendor.name} camera")).strip()
        device_key = str(body.get("device_key", "")).strip()
        if not email:
            raise web.HTTPBadRequest(text="email is required")

        try:
            camera_id = self.vendor.camera_id(device_key)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

        existing = store.find(camera_id)
        if existing:
            # Idempotent: the backend call below mints NEW camera rows, so
            # re-provisioning a known unit must never reach it.
            return web.json_response({
                "ok": True, "already_provisioned": True,
                "camera": self.vendor.public_fields(existing)})

        try:
            result = await asyncio.to_thread(
                post_json,
                f"{self.settings['backend_url']}/v1/gateway/provision-camera",
                {"email": email, "room": name, "icon_name": "tv"},
                {"Authorization": f"Bearer {self.settings['gateway_token']}"},
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise web.HTTPBadGateway(
                text=f"backend provisioning failed: {exc.code} {detail}") from exc

        identities = [c["livekit_identity"] for c in result.get("cameras", [])]
        if not identities:
            raise web.HTTPBadGateway(text="backend returned no camera identities")

        entry = self.vendor.entry_from_provision(body, identities, name)
        store.add(entry)
        log(f"[{camera_id}] provisioned with {len(identities)} identity mapping(s)")
        return web.json_response(
            {"ok": True, "camera": self.vendor.public_fields(entry)})

    async def desired(self, request: web.Request) -> web.Response:
        require_local(request)
        camera = store.find(request.match_info["camera_id"])
        if not camera:
            raise web.HTTPNotFound(text="camera not found")
        spec = self.roster.publisher_for(camera)
        return web.json_response({
            "publisher": spec.to_json() if spec else None,
            "roster_error": self.roster.error,
            "updated_at": self.roster.updated_at,
        })

    async def token(self, request: web.Request) -> web.Response:
        """Mint a LiveKit token, but only for a publisher the backend
        currently wants. Otherwise this route is a way to join any room."""
        require_local(request)
        camera = store.find(request.match_info["camera_id"])
        if not camera:
            raise web.HTTPNotFound(text="camera not found")
        body = await request.json()
        identity = str(body.get("identity", ""))
        room = str(body.get("room", ""))
        spec = self.roster.publisher_for(camera)
        if not spec or not spec.connected or (identity, room) != (spec.identity, spec.room):
            raise web.HTTPForbidden(text="publisher is not desired/authorized")
        return web.json_response({"url": self.settings["livekit_url"],
                                  "token": self.mint_token(identity, room)})

    async def update_status(self, request: web.Request) -> web.Response:
        require_local(request)
        camera_id = request.match_info["camera_id"]
        if not store.find(camera_id):
            raise web.HTTPNotFound(text="camera not found")
        self.vendor.report(camera_id, await request.json())
        return web.json_response({"ok": True})

    async def cameras(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "cameras": [self.vendor.public_fields(c) for c in store.cameras()]})

    async def health(self, request: web.Request) -> web.Response:
        """Liveness for anyone; inventory only for an authorized caller.

        The detail below carries LiveKit room names, which are
        `patient_<uuid>`. A monitoring probe does not need the patient
        list, and an open endpoint that returns one hands over a roster
        of who we watch.
        """
        configured = store.cameras()
        summary = {
            "ok": self.roster.error is None,
            "vendor": self.vendor.name,
            "configured_cameras": len(configured),
            "roster_cameras": len(self.roster.items),
            "roster_error": self.roster.error,
            "stream_mode": self.settings["stream_mode"],
        }
        try:
            require_local(request)
        except web.HTTPForbidden:
            return web.json_response(summary)
        return web.json_response({
            **summary,
            "capabilities": list(self.vendor.capabilities),
            "stream_policy": {
                c["camera_id"]: (lambda s: s.to_json() if s else None)(
                    self.roster.publisher_for(c))
                for c in configured
            },
            # Per ENTITY, never per process: one row per camera, so a
            # camera whose worker died ten minutes ago cannot hide behind
            # the fact that the bridge itself is still alive.
            #
            # The SAME rows the fleet poll sends. Building them twice
            # would let the local view and the backend's view disagree
            # about a camera, which is the exact confusion this endpoint
            # exists to resolve.
            "entities": self.fleet_report()["entities"],
        })


async def create_app(bridge: Bridge) -> web.Application:
    async def admin_page(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(Path(__file__).resolve().parent / "admin.html")

    app = web.Application(client_max_size=128 * 1024)
    app["bridge"] = bridge
    app.router.add_get("/", admin_page)
    app.router.add_get("/api/health", bridge.health)
    app.router.add_get("/api/cameras", bridge.cameras)
    app.router.add_post("/api/provision", bridge.provision)
    app.router.add_get("/api/runtime/{camera_id}/desired", bridge.desired)
    app.router.add_post("/api/runtime/{camera_id}/token", bridge.token)
    app.router.add_post("/api/runtime/{camera_id}/status", bridge.update_status)
    # Vendor routes and static mounts go LAST: some vendor SDKs resolve
    # assets against the document root, so their catch-all must not
    # shadow the API above.
    bridge.vendor.add_routes(app, bridge)
    for prefix, directory in bridge.vendor.static_mounts().items():
        app.router.add_static(prefix, directory, show_index=False,
                              follow_symlinks=False)
    return app
