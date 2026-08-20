#!/usr/bin/env python3
"""Warner H5 SDK -> LiveKit gateway bridge.

Python owns provisioning, the backend roster, LiveKit token minting, persistence,
health, and Chromium supervision. A local headless Chromium page executes the
vendor H5 SDK because its P2P transport/decoder is JavaScript + WebAssembly.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import signal
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web
from livekit import api


HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get(
    "WARNER_BRIDGE_CONFIG", HERE / "warner_cameras.json"))
GATEWAY_CONFIG_PATH = Path(os.environ.get(
    "WARNER_GATEWAY_CONFIG", HERE / "cameras.json"))
STATIC_DIR = HERE / "warner_bridge_static"
SDK_DIR = Path(os.environ.get("WARNER_SDK_DIR", HERE / "warner_sdk"))
# Loopback by DEFAULT (2026-08-20). This used to default to 0.0.0.0, so
# the API's only protection was a peer-address check — one `docker run -p`
# or one cloud VM away from being published to the internet. Opt in with
# WARNER_BRIDGE_HOST when you actually mean to listen wider, and pair it
# with WARNER_BRIDGE_KEY.
HOST = os.environ.get("WARNER_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("WARNER_BRIDGE_PORT", "8090"))
# Shared secret for every mutating/credential-bearing route. Empty means
# "loopback only", which is the safe default for a box where the runtime
# pages are the sole caller.
API_KEY = os.environ.get("WARNER_BRIDGE_KEY", "")
# Tailnet CGNAT range. Tailscale hands every node a 100.64.0.0/10
# address, so this is "reachable over our private mesh" — used as a
# SECOND factor beside the key, never as authentication on its own
# (anything that can spoof a source address can claim to be a peer).
TAILNET_CIDR = ipaddress.ip_network("100.64.0.0/10")
ALLOW_TAILNET = os.environ.get("WARNER_BRIDGE_ALLOW_TAILNET", "") == "1"
PUBLIC_ORIGIN = os.environ.get("WARNER_BRIDGE_ORIGIN", f"http://127.0.0.1:{PORT}")
# Capture canvas default: 3MP, matching the sensor. The runtime markup
# hardcoded 1280x720, so every camera was downscaled before LiveKit saw
# it — detail these units actually captured, discarded in the one place
# nothing downstream can recover it. `stream_id: 0` is already the main
# (not sub) stream, so this is the last thing standing between the
# sensor and the room. The runtime still adopts the camera's own frame
# size on its first frame, so a unit that streams something else wins.
CAPTURE_WIDTH = int(os.environ.get("WARNER_CAPTURE_WIDTH", "2304"))
CAPTURE_HEIGHT = int(os.environ.get("WARNER_CAPTURE_HEIGHT", "1296"))
FRAME_STALL_SECONDS = float(os.environ.get("WARNER_FRAME_STALL_SECONDS", "20"))
FRAME_STARTUP_GRACE_SECONDS = float(os.environ.get(
    "WARNER_FRAME_STARTUP_GRACE_SECONDS", "45"))


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def redact_browser_log(message: str) -> str:
    """Remove short-lived LiveKit credentials from browser console URLs."""
    return re.sub(
        r"([?&](?:access_token|join_request)=)[^&\s}]+",
        r"\1<redacted>",
        message,
    )


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def load_gateway_settings() -> dict[str, Any]:
    cfg = load_json(GATEWAY_CONFIG_PATH, {})
    return {
        "backend_url": os.environ.get("BACKEND_URL")
        or os.environ.get("WARNER_BACKEND_URL")
        or cfg.get("backend_url", ""),
        "gateway_token": os.environ.get("GATEWAY_TOKEN")
        or os.environ.get("WARNER_GATEWAY_TOKEN")
        or cfg.get("gateway_token", ""),
        "livekit_url": os.environ.get("LIVEKIT_URL")
        or os.environ.get("WARNER_LIVEKIT_URL")
        or cfg.get("livekit_url", ""),
        "livekit_api_key": os.environ.get("LIVEKIT_API_KEY")
        or os.environ.get("WARNER_LIVEKIT_API_KEY")
        or cfg.get("livekit_api_key", ""),
        "livekit_api_secret": os.environ.get("LIVEKIT_API_SECRET")
        or os.environ.get("WARNER_LIVEKIT_API_SECRET")
        or cfg.get("livekit_api_secret", ""),
        "poll_seconds": float(cfg.get("poll_seconds", 3)),
        # Production requirement: provisioned Warner cameras publish
        # continuously so the AI backend can subscribe while the mobile app is
        # closed. Use "policy" only when the backend provides a reliable
        # combined viewer/AI desired state.
        "stream_mode": str(cfg.get("stream_mode", "continuous")).lower(),
    }


def validate_settings(settings: dict[str, Any]) -> None:
    missing = [key for key in (
        "backend_url", "gateway_token", "livekit_url",
        "livekit_api_key", "livekit_api_secret",
    ) if not settings.get(key)]
    if missing:
        raise RuntimeError(f"missing gateway settings: {', '.join(missing)}")
    if not SDK_DIR.joinpath("connector.js").exists():
        raise RuntimeError(
            f"Warner SDK not found at {SDK_DIR}; set WARNER_SDK_DIR")
    if not SDK_DIR.joinpath("js", "livekit-client.umd.js").exists():
        raise RuntimeError(
            "warner_sdk/js/livekit-client.umd.js is required")


def read_bridge_config() -> dict[str, Any]:
    return load_json(CONFIG_PATH, {"version": 1, "cameras": []})


def write_bridge_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{CONFIG_PATH.name}.", dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, CONFIG_PATH)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def physical_id(device_key: str) -> str:
    parts = device_key.split(":")
    if len(parts) != 3 or not all(parts):
        raise ValueError("Warner identity must be productId:deviceId:deviceSecret")
    stable = f"{parts[0]}:{parts[1]}".encode()
    return "warner_" + hashlib.sha256(stable).hexdigest()[:16]


def public_camera(camera: dict[str, Any]) -> dict[str, Any]:
    return {
        "camera_id": camera["camera_id"],
        "device_id": camera["device_id"],
        "name": camera.get("name", "Warner camera"),
        "channel": camera.get("channel", 0),
        "stream_id": camera.get("stream_id", 0),
        "livekit_identities": camera.get("livekit_identities", []),
    }


def post_json(url: str, body: dict[str, Any], headers=None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode())


def get_json(url: str, headers=None, timeout=10) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


class RosterCache:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.cameras: list[dict[str, Any]] = []
        self.error: str | None = None
        self.updated_at = 0.0

    def fetch(self) -> list[dict[str, Any]]:
        data = get_json(
            f"{self.settings['backend_url']}/v1/gateway/roster",
            {"Authorization": f"Bearer {self.settings['gateway_token']}"},
        )
        return data.get("cameras", [])

    async def run(self) -> None:
        while True:
            try:
                self.cameras = await asyncio.to_thread(self.fetch)
                self.error = None
                self.updated_at = time.time()
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                log(f"roster poll failed: {exc}")
            await asyncio.sleep(self.settings["poll_seconds"])

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "active", "enabled"}
        return bool(value)

    @classmethod
    def stream_policy(cls, item: dict[str, Any]) -> tuple[bool, list[str]]:
        """Return effective publishing state and the reasons requiring it.

        `desired_state=connected` remains the authoritative, backwards-
        compatible backend signal. Newer backends may additionally expose
        viewer_count/mobile_active and ai_enabled/ai_paused. AI keeps the
        publisher alive independently of mobile viewing.
        """
        reasons: list[str] = []
        if str(item.get("desired_state", "")).lower() == "connected":
            reasons.append("backend_desired")
        try:
            if int(item.get("viewer_count", 0) or 0) > 0:
                reasons.append("mobile_viewer")
        except (TypeError, ValueError):
            pass
        if cls._truthy(item.get("mobile_active", False)):
            reasons.append("mobile_viewer")

        ai_enabled = cls._truthy(
            item.get("ai_enabled", item.get("analysis_enabled", False)))
        ai_paused = cls._truthy(
            item.get("ai_paused", item.get("analysis_paused", False)))
        if ai_enabled and not ai_paused:
            reasons.append("ai_analysis")
        return bool(reasons), sorted(set(reasons))

    def desired_for(self, camera: dict[str, Any]) -> list[dict[str, Any]]:
        allowed = set(camera.get("livekit_identities", []))
        desired = []
        for item in self.cameras:
            if item.get("livekit_identity") not in allowed:
                continue
            if self.settings["stream_mode"] == "continuous":
                required, reasons = True, ["continuous_bridge"]
            else:
                required, reasons = self.stream_policy(item)
            desired.append({
                "identity": item["livekit_identity"],
                "room": item["livekit_room"],
                "desired_state": "connected" if required else "disconnected",
                "stream_reasons": reasons,
            })
        return desired


@dataclass
class WorkerStatus:
    state: str = "starting"
    detail: str = ""
    updated_at: float = 0.0
    last_frame_at: float = 0.0
    frame_count: int = 0
    restart_count: int = 0
    last_restart_at: float = 0.0


class ChromiumSupervisor:
    def __init__(self, roster: RosterCache):
        self.roster = roster
        self.playwright = None
        self.browser = None
        self.contexts: dict[str, Any] = {}
        self.pages: dict[str, Any] = {}
        self.status: dict[str, WorkerStatus] = {}
        self.started_at: dict[str, float] = {}
        self.restart_counts: dict[str, int] = {}
        self.last_restart_at: dict[str, float] = {}

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is required: pip install playwright && "
                "playwright install chromium") from exc

        self.playwright = await async_playwright().start()
        executable = os.environ.get("WARNER_CHROMIUM_EXECUTABLE") or None
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            executable_path=executable,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        log("headless Chromium started")

    async def new_camera_context(self) -> Any:
        # A separate context keeps every Warner page visible. The SDK pauses
        # decoding on visibilitychange, so multiple tabs in one context leave
        # only the active camera producing video frames.
        context = await self.browser.new_context(
            viewport={"width": CAPTURE_WIDTH, "height": CAPTURE_HEIGHT},
        )
        # Warner's WebCodecs path transfers the source canvas to an
        # OffscreenCanvas. Force its WASM decoder so canvas.captureStream()
        # receives the rendered frames in headless Chromium.
        await context.add_init_script("""
            Object.defineProperty(globalThis, "VideoDecoder", {
                value: undefined,
                configurable: false,
                writable: false
            });
        """)
        return context

    async def reconcile(self) -> None:
        # Continuous mode keeps provisioned camera publishers alive for mobile
        # and AI subscribers. Policy mode is available for backends that expose
        # a reliable combined viewer/AI state. One physical worker fans out to
        # every assigned LiveKit room without opening duplicate Warner streams.
        wanted = {
            camera["camera_id"]
            for camera in read_bridge_config().get("cameras", [])
            if any(
                publisher["desired_state"] == "connected"
                for publisher in self.roster.desired_for(camera)
            )
        }
        for camera_id in list(self.pages):
            if camera_id not in wanted:
                await self.pages.pop(camera_id).close()
                context = self.contexts.pop(camera_id, None)
                if context:
                    await context.close()
                self.status.pop(camera_id, None)
                self.started_at.pop(camera_id, None)
                self.restart_counts.pop(camera_id, None)
                self.last_restart_at.pop(camera_id, None)
                log(f"[{camera_id}] browser worker stopped")

        for camera_id in wanted:
            if camera_id in self.pages:
                continue
            context = await self.new_camera_context()
            page = await context.new_page()
            page.on("console", lambda msg, cid=camera_id:
                    log(f"[{cid}] browser {msg.type}: "
                        f"{redact_browser_log(msg.text)}"))
            page.on("pageerror", lambda exc, cid=camera_id:
                    log(f"[{cid}] browser error: {exc}"))
            page.on("response", lambda response, cid=camera_id:
                    log(f"[{cid}] browser HTTP {response.status}: "
                        f"{redact_browser_log(response.url)}")
                    if response.status >= 400 else None)
            await page.goto(
                f"{PUBLIC_ORIGIN}/runtime?camera_id={camera_id}",
                wait_until="domcontentloaded",
            )
            self.pages[camera_id] = page
            self.contexts[camera_id] = context
            now = time.time()
            self.started_at[camera_id] = now
            self.status[camera_id] = WorkerStatus(
                updated_at=now,
                restart_count=self.restart_counts.get(camera_id, 0),
                last_restart_at=self.last_restart_at.get(camera_id, 0.0),
            )
            log(f"[{camera_id}] browser worker started")

    async def restart_stalled_workers(self) -> None:
        now = time.time()
        for camera_id in list(self.pages):
            status = self.status.get(camera_id, WorkerStatus())
            started = self.started_at.get(camera_id, now)
            if status.last_frame_at:
                frame_age = now - status.last_frame_at
                if frame_age <= FRAME_STALL_SECONDS:
                    continue
                reason = (
                    f"no video frame for {frame_age:.1f}s "
                    f"(limit {FRAME_STALL_SECONDS:.0f}s)"
                )
            else:
                startup_age = now - started
                if startup_age <= FRAME_STARTUP_GRACE_SECONDS:
                    continue
                reason = (
                    f"no first video frame after {startup_age:.1f}s "
                    f"(limit {FRAME_STARTUP_GRACE_SECONDS:.0f}s)"
                )

            count = self.restart_counts.get(camera_id, 0) + 1
            self.restart_counts[camera_id] = count
            self.last_restart_at[camera_id] = now
            log(f"[{camera_id}] VIDEO STALLED: {reason}; "
                f"recreating browser worker (restart {count})")

            page = self.pages.pop(camera_id, None)
            context = self.contexts.pop(camera_id, None)
            self.started_at.pop(camera_id, None)
            if page:
                try:
                    await page.close()
                except Exception as exc:
                    log(f"[{camera_id}] stalled page close ignored: {exc}")
            if context:
                try:
                    await context.close()
                except Exception as exc:
                    log(f"[{camera_id}] stalled context close ignored: {exc}")
            self.status[camera_id] = WorkerStatus(
                state="video_stalled",
                detail=reason,
                updated_at=now,
                restart_count=count,
                last_restart_at=now,
            )

    async def run(self) -> None:
        await self.start()
        while True:
            try:
                await self.reconcile()
                await self.restart_stalled_workers()
            except Exception as exc:  # noqa: BLE001
                log(f"worker reconcile failed: {exc}")
            await asyncio.sleep(2)

    async def close(self) -> None:
        for context in list(self.contexts.values()):
            try:
                await context.close()
            except Exception as exc:  # browser may already have disposed it
                log(f"browser context close ignored: {exc}")
        self.contexts.clear()
        if self.browser:
            try:
                await self.browser.close()
            except Exception as exc:
                log(f"browser close ignored: {exc}")
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception as exc:
                log(f"Playwright stop ignored: {exc}")


class WarnerBridge:
    def __init__(self):
        self.settings = load_gateway_settings()
        validate_settings(self.settings)
        self.roster = RosterCache(self.settings)
        self.chromium = ChromiumSupervisor(self.roster)

    def find_camera(self, camera_id: str) -> dict[str, Any] | None:
        return next((c for c in read_bridge_config().get("cameras", [])
                     if c.get("camera_id") == camera_id), None)

    @staticmethod
    def require_local(request: web.Request) -> None:
        """Authorize an API call. Secret first, network scope second.

        The old version trusted `request.remote` alone. That is the TCP
        peer, so behind ANY reverse proxy — nginx, Caddy, a cloud load
        balancer — it reads as the proxy's address, usually 127.0.0.1,
        and the check silently became allow-all. Hosting this anywhere
        reachable is exactly when it stopped working. It also treated
        `None` (unknown peer) as trusted, which is backwards.

        So: when WARNER_BRIDGE_KEY is set it is REQUIRED, and the peer
        check becomes a second layer rather than the only one. Without a
        key the service stays loopback-only, which is correct for a box
        where the runtime pages are the sole caller.
        """
        if API_KEY:
            supplied = request.headers.get("x-bridge-key", "")
            if not hmac.compare_digest(supplied, API_KEY):
                raise web.HTTPForbidden(text="bad or missing x-bridge-key")

        peer = request.remote
        if peer in {"127.0.0.1", "::1"}:
            return
        if peer is None:
            # Unknown peer is not a trusted peer.
            raise web.HTTPForbidden(text="unidentifiable peer")
        if ALLOW_TAILNET:
            try:
                if ipaddress.ip_address(peer) in TAILNET_CIDR:
                    return
            except ValueError:
                pass
        if API_KEY:
            # A valid key from off-box is allowed on purpose — that is
            # what the key is for. The peer check above just means an
            # unkeyed deployment can never be reached remotely.
            return
        raise web.HTTPForbidden(text="runtime API is loopback-only")

    async def provision(self, request: web.Request) -> web.Response:
        # Was UNAUTHENTICATED (2026-08-20). Every other route called
        # require_local; this one did not, and it is the one that reaches
        # the backend with our gateway token and writes camera config. An
        # email plus network reach was enough to provision a camera onto
        # someone's account.
        self.require_local(request)
        body = await request.json()
        email = str(body.get("email", "")).strip()
        name = str(body.get("name", "Warner camera")).strip()
        device_key = str(body.get("device_key", "")).strip()
        camera_user = str(body.get("camera_user", "admin"))
        camera_password = str(body.get("camera_password", ""))
        channel = int(body.get("channel", 0))
        stream_id = int(body.get("stream_id", 0))

        if not email:
            raise web.HTTPBadRequest(text="email is required")
        camera_id = physical_id(device_key)
        device_id = device_key.split(":")[1]
        cfg = read_bridge_config()
        existing = next((c for c in cfg.get("cameras", [])
                         if c.get("camera_id") == camera_id), None)

        if existing:
            return web.json_response({
                "ok": True, "already_provisioned": True,
                "camera": public_camera(existing),
            })

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
            raise web.HTTPBadGateway(
                text="backend provisioning returned no camera identities")

        entry = {
            "camera_id": camera_id,
            "device_id": device_id,
            "device_key": device_key,
            "name": name,
            "camera_user": camera_user,
            "camera_password": camera_password,
            "channel": channel,
            "stream_id": stream_id,
            "livekit_identities": identities,
        }
        cfg.setdefault("version", 1)
        cfg.setdefault("cameras", []).append(entry)
        write_bridge_config(cfg)
        log(f"[{camera_id}] provisioned with {len(identities)} identity mapping(s)")
        return web.json_response({"ok": True, "camera": public_camera(entry)})

    async def runtime_config(self, request: web.Request) -> web.Response:
        # This response carries the camera's OWN credentials — device
        # secret and admin password — so it is the highest-value target
        # on the bridge, and the runtime page genuinely needs them to
        # drive the vendor SDK. It is only ever fetched by our own
        # headless page over loopback, so it stays behind require_local
        # AND is refused outright to a remote caller even with a valid
        # key: a leaked key should not also mean leaked camera logins.
        self.require_local(request)
        if request.remote not in {"127.0.0.1", "::1"}:
            raise web.HTTPForbidden(
                text="runtime config is loopback-only (carries credentials)")
        camera = self.find_camera(request.match_info["camera_id"])
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
            "livekit_url": self.settings["livekit_url"],
            # Starting size for the capture canvas. The runtime adopts the
            # camera's true frame size on the first frame, so this only
            # decides what we publish before that arrives — but it must
            # not be 720p on a 3MP camera, which is what the markup
            # hardcoded. Per-camera override wins over the global default.
            "capture_width": int(
                camera.get("capture_width", CAPTURE_WIDTH)),
            "capture_height": int(
                camera.get("capture_height", CAPTURE_HEIGHT)),
        })

    async def desired(self, request: web.Request) -> web.Response:
        self.require_local(request)
        camera = self.find_camera(request.match_info["camera_id"])
        if not camera:
            raise web.HTTPNotFound(text="camera not found")
        return web.json_response({
            "publishers": self.roster.desired_for(camera),
            "roster_error": self.roster.error,
            "updated_at": self.roster.updated_at,
        })

    async def token(self, request: web.Request) -> web.Response:
        self.require_local(request)
        camera = self.find_camera(request.match_info["camera_id"])
        if not camera:
            raise web.HTTPNotFound(text="camera not found")
        body = await request.json()
        identity = str(body.get("identity", ""))
        room = str(body.get("room", ""))
        allowed = {
            (p["identity"], p["room"])
            for p in self.roster.desired_for(camera)
            if p["desired_state"] == "connected"
        }
        if (identity, room) not in allowed:
            raise web.HTTPForbidden(text="publisher is not desired/authorized")
        token = (api.AccessToken(
            self.settings["livekit_api_key"],
            self.settings["livekit_api_secret"],
        ).with_identity(identity).with_name(identity)
            .with_grants(api.VideoGrants(room_join=True, room=room))
            .to_jwt())
        return web.json_response({
            "url": self.settings["livekit_url"], "token": token})

    async def update_status(self, request: web.Request) -> web.Response:
        self.require_local(request)
        camera_id = request.match_info["camera_id"]
        if not self.find_camera(camera_id):
            raise web.HTTPNotFound(text="camera not found")
        body = await request.json()
        now = time.time()
        previous = self.chromium.status.get(camera_id, WorkerStatus())
        state = str(body.get("state", "unknown"))[:80]
        if state == "frame_heartbeat":
            previous.last_frame_at = now
            try:
                previous.frame_count = max(
                    previous.frame_count, int(body.get("frame_count", 0)))
            except (TypeError, ValueError):
                pass
            previous.updated_at = now
            self.chromium.status[camera_id] = previous
        else:
            previous.state = state
            previous.detail = str(body.get("detail", ""))[:500]
            previous.updated_at = now
            self.chromium.status[camera_id] = previous
        return web.json_response({"ok": True})

    async def health(self, request: web.Request) -> web.Response:
        """Liveness for anyone; inventory only for an authorized caller.

        This used to return `stream_policy` unauthenticated, and those
        entries carry LiveKit room names — which are `patient_<uuid>`.
        A monitoring probe does not need the patient list, and putting
        one on an open endpoint hands over a roster of who we watch.
        """
        cfg = read_bridge_config()
        configured = cfg.get("cameras", [])
        summary = {
            "ok": self.roster.error is None,
            "configured_cameras": len(configured),
            "roster_cameras": len(self.roster.cameras),
            "roster_error": self.roster.error,
            "stream_mode": self.settings["stream_mode"],
        }
        try:
            self.require_local(request)
        except web.HTTPForbidden:
            return web.json_response(summary)
        return web.json_response({
            **summary,
            "sdk_dir": str(SDK_DIR),
            "stream_policy": {
                camera["camera_id"]: self.roster.desired_for(camera)
                for camera in configured
            },
            "workers": {
                cid: {
                    "state": status.state,
                    "detail": status.detail,
                    "updated_at": status.updated_at,
                    "last_frame_at": status.last_frame_at or None,
                    "frame_age_seconds": (
                        round(max(0.0, time.time() - status.last_frame_at), 1)
                        if status.last_frame_at else None
                    ),
                    "frame_count": status.frame_count,
                    "restart_count": status.restart_count,
                    "last_restart_at": status.last_restart_at or None,
                }
                for cid, status in self.chromium.status.items()
            },
        })

    async def cameras(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "cameras": [public_camera(c)
                        for c in read_bridge_config().get("cameras", [])]})

    async def debug_screenshot(self, request: web.Request) -> web.Response:
        self.require_local(request)
        camera_id = request.match_info["camera_id"]
        page = self.chromium.pages.get(camera_id)
        if not page:
            raise web.HTTPNotFound(text="camera worker is not active")
        image = await page.screenshot(type="png")
        return web.Response(
            body=image,
            content_type="image/png",
            headers={"Cache-Control": "no-store"},
        )


async def create_app(bridge: WarnerBridge) -> web.Application:
    async def admin_page(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "admin.html")

    async def runtime_page(request: web.Request) -> web.FileResponse:
        bridge.require_local(request)
        return web.FileResponse(STATIC_DIR / "runtime.html")

    app = web.Application(client_max_size=128 * 1024)
    app["bridge"] = bridge
    app.router.add_get("/", admin_page)
    app.router.add_get("/runtime", runtime_page)
    app.router.add_get("/api/health", bridge.health)
    app.router.add_get("/api/cameras", bridge.cameras)
    app.router.add_get(
        "/api/debug/{camera_id}/screenshot", bridge.debug_screenshot)
    app.router.add_post("/api/provision", bridge.provision)
    app.router.add_get("/api/runtime/{camera_id}/config", bridge.runtime_config)
    app.router.add_get("/api/runtime/{camera_id}/desired", bridge.desired)
    app.router.add_post("/api/runtime/{camera_id}/token", bridge.token)
    app.router.add_post("/api/runtime/{camera_id}/status", bridge.update_status)
    app.router.add_static("/static", STATIC_DIR, show_index=False)
    app.router.add_static("/sdk", SDK_DIR, show_index=False, follow_symlinks=False)
    # Several Warner bundles resolve workers/WASM/images against the document
    # root instead of their script URL (for example /j-decoder.wasm,
    # /mqtt/mqttWorker.js and /logo_bg.png). Keep the explicit application/API
    # routes above, then serve unmatched root-relative vendor assets here.
    app.router.add_static("/", SDK_DIR, show_index=False, follow_symlinks=False)
    return app


async def main() -> None:
    bridge = WarnerBridge()
    app = await create_app(bridge)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    log(f"Warner bridge admin: http://{HOST}:{PORT}/")
    roster_task = asyncio.create_task(bridge.roster.run())
    chromium_task = asyncio.create_task(bridge.chromium.run())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        roster_task.cancel()
        chromium_task.cancel()
        await asyncio.gather(roster_task, chromium_task, return_exceptions=True)
        await bridge.chromium.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
