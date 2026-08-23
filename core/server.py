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
from aiohttp.web_urldispatcher import StaticResource

from core import auth, fleet, store
from core.auth import require_admin, require_asset, require_runtime
from core.config import FRAME_STALL_SECONDS, SERVICE_KIND, VERSION
from core.identity import fingerprint
from core.roster import Roster
from core.util import log, post_json, redact
from vendors.base import Publisher, Vendor


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
            kind=SERVICE_KIND,
            version=VERSION,
            capabilities=self.vendor.capabilities,
            entities=self._health_by_backend_id(),
            now=time.time(),
            stall_seconds=FRAME_STALL_SECONDS,
        )

    def _health_by_backend_id(self) -> dict[str, Any]:
        """Health keyed by the BACKEND's camera uuid.

        Contract rule 3 says an entity id is `<camera uuid>`. We were
        sending our own vendor key ("warner_8d2506f65170d9f3"), which the
        backend fed straight into a uuid[] bind — so every poll 500'd on
        `invalid UUID ... got 23`, and because ONE malformed id fails the
        whole statement, no camera on this bridge ever had its heartbeat
        stamped. `last_heartbeat_at` stayed NULL while the camera was
        plainly publishing: healthy by every other measure, and invisible
        to the staleness sweep that is supposed to notice a dead one.

        `livekit_identity` is the join: it is the one field both sides
        agree on, ours from provisioning and theirs from the roster.
        An entity we cannot map is DROPPED rather than sent under a made-up
        id — a camera the backend has forgotten should read as absent,
        not as a stranger.
        """
        by_identity = {
            c.get("livekit_identity"): c.get("camera_id")
            for c in (self.roster.items or [])
            if c.get("livekit_identity") and c.get("camera_id")
        }
        mapped: dict[str, Any] = {}
        for vendor_key, health in self.vendor.health().items():
            configured = store.find(vendor_key) or {}
            backend_id = by_identity.get(configured.get("livekit_identity"))
            if backend_id:
                mapped[backend_id] = health
        return mapped

    async def mint_token(self, identity: str, room: str) -> str:
        """Ask the BACKEND for a token. We hold no signing secret.

        Signing our own meant every bridge box carried the project-wide
        LiveKit API secret, which mints a token for any room — that is,
        any patient's live video — on a host whose whole job is talking
        to a camera vendor. The gateway already refuses to do this; its
        own comment says the box "must NOT fall back to signing its own,
        because it no longer has a secret to sign with".

        The backend checks that WE own the camera named by `identity` and
        that `room` is that camera's own room, so a compromised bridge
        can still only publish as its own cameras. Blocking urllib in a
        thread, matching the poller's style: one short call per connect,
        not a hot path.
        """
        body = await asyncio.to_thread(
            post_json,
            f"{self.settings['backend_url']}/v1/livekit/token",
            {"room": room, "identity": identity, "role": "gateway"},
            {"Authorization": f"Bearer {self.settings['gateway_token']}"},
        )
        return body["token"]

    # `enroll()` and `enroll_stored()` lived here until 2026-08-23.
    # `/v1/gateway/enroll` is gone: a service binds to an account by
    # redeeming a setup code at provision time, so there is no longer a
    # second way to become known — and nothing for a boot-time self-heal
    # to do. `service_kind` still rides on every provision and on every
    # fleet poll, which is where the backend re-reads it.

    def plan(self) -> dict[str, Publisher | None]:
        return self.roster.plan(store.cameras())

    async def run_vendor(self) -> None:
        """Reconcile the vendor to the backend's wishes, forever."""
        started = False
        while True:
            try:
                # INSIDE the loop. This used to run once, above it, so a
                # failure here killed the task outright and nothing ever
                # retried or reported: the bridge answered /api/health,
                # polled the roster, showed its cameras, and published
                # nothing, forever. A missing Chromium (PLAYWRIGHT_BROWSERS_PATH
                # unset) is exactly that shape, and it reads as healthy.
                #
                # Retried because the causes are transient or fixable in
                # place — a browser still installing, a machine still
                # booting. `started` keeps it to once per success.
                if not started:
                    await self.vendor.start()
                    started = True
                await self.vendor.apply(self.plan())
            except Exception as exc:  # noqa: BLE001 — one bad pass must not end the loop
                # Redacted because a failed page load reports the URL it
                # was opening, and that URL carries the page's capability.
                log(redact(f"vendor reconcile failed: {exc}"))
            await asyncio.sleep(2)

    # ── routes ────────────────────────────────────────────────────
    async def provision(self, request: web.Request) -> web.Response:
        """Claim a physical camera onto a caregiver account.

        Authorized because this is the route that reaches the backend
        with our gateway token and then writes camera credentials to
        disk; it was briefly open, which made an email plus network
        reach enough to provision onto someone's account.
        """
        require_admin(request)
        body = await request.json()
        pairing_code = str(body.get("pairing_code", "")).strip()
        email = str(body.get("email", "")).strip()
        name = str(body.get("name", f"{self.vendor.name} camera")).strip()
        device_key = str(body.get("device_key", "")).strip()
        if not pairing_code:
            # The code proves the account; an email does not.
            raise web.HTTPBadRequest(text="setup code is required")

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
                # service_kind on the common path too: this call is what
                # creates our gateways row on a fresh install, and a row
                # created without it is a single-tenant box.
                {"pairing_code": pairing_code, "email": email or None,
                 "room": name, "icon_name": "tv",
                 "service_kind": SERVICE_KIND},
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
        # Remembered only now, because the backend has just proved the
        # email names a real account. A typo replayed at every boot would
        # be a bridge repeatedly declaring itself to the wrong customer.
        store.set_account_email(email)
        log(f"[{camera_id}] provisioned with {len(identities)} identity mapping(s)")
        return web.json_response(
            {"ok": True, "camera": self.vendor.public_fields(entry)})

    async def desired(self, request: web.Request) -> web.Response:
        require_runtime(request, request.match_info["camera_id"])
        camera = store.find(request.match_info["camera_id"])
        if not camera:
            raise web.HTTPNotFound(text="camera not found")
        spec = self.roster.publisher_for(camera)
        return web.json_response({
            "publisher": spec.to_json() if spec else None,
            "roster_error": self.roster.error,
            "updated_at": self.roster.updated_at,
        })

    async def update_status(self, request: web.Request) -> web.Response:
        camera_id = request.match_info["camera_id"]
        require_runtime(request, camera_id)
        if not store.find(camera_id):
            raise web.HTTPNotFound(text="camera not found")
        self.vendor.report(camera_id, await request.json())
        return web.json_response({"ok": True})

    async def cameras(self, request: web.Request) -> web.Response:
        """The device inventory: camera_id, device_id, name and LiveKit
        identity for every customer this bridge serves. It was the one
        route with no check at all."""
        require_admin(request)
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
            require_admin(request)
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


@web.middleware
async def guard_assets(request: web.Request, handler: Any) -> web.StreamResponse:
    """Static mounts are the one surface with no handler of ours to put a
    check in, so anyone who could reach the port could walk them."""
    if isinstance(request.match_info.route.resource, StaticResource):
        require_asset(request)
    return await handler(request)


async def create_app(bridge: Bridge) -> web.Application:
    async def admin_page(request: web.Request) -> web.FileResponse:
        # The operator arrives at `/?key=…` because a browser cannot put
        # a header on a navigation; the cookie handed back is what the
        # framed SDK and the provision fetch below it then carry.
        require_admin(request)
        # Read and returned as a plain body rather than FileResponse.
        # FileResponse attaches ETag + Last-Modified during prepare() and
        # answers a conditional request with 304 no matter what
        # Cache-Control says — so a refresh kept whatever console the
        # browser already had. Setting no-store alone only worked for
        # clients polite enough to stop revalidating; sending no
        # validator at all leaves nothing to revalidate against.
        #
        # An installer driving a stale console is the real cost: it can
        # carry fields we have since removed, or lack the fix we shipped
        # because the old one misled them. The page is a few KB and read
        # once per install.
        body = (Path(__file__).resolve().parent / "admin.html").read_bytes()
        response = web.Response(
            body=body, content_type="text/html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )
        return auth.hand_back(request, response)

    app = web.Application(client_max_size=128 * 1024,
                          middlewares=[guard_assets])
    app["bridge"] = bridge
    app.router.add_get("/", admin_page)
    app.router.add_get("/api/health", bridge.health)
    app.router.add_get("/api/cameras", bridge.cameras)
    app.router.add_post("/api/provision", bridge.provision)
    app.router.add_get("/api/runtime/{camera_id}/desired", bridge.desired)
    app.router.add_post("/api/runtime/{camera_id}/status", bridge.update_status)
    # Vendor routes and static mounts go LAST: some vendor SDKs resolve
    # assets against the document root, so their catch-all must not
    # shadow the API above.
    bridge.vendor.add_routes(app, bridge)
    for prefix, directory in bridge.vendor.static_mounts().items():
        app.router.add_static(prefix, directory, show_index=False,
                              follow_symlinks=False)
    return app
