#!/usr/bin/env python3
"""Headless Chromium, one page per camera.

The browser is here for ONE reason: the vendor's cloud/P2P transport and
auth are JavaScript and WebAssembly with no native SDK. Everything else
it currently does — decoding, painting a 3MP canvas, re-encoding — is
being moved out to native ffmpeg and the LiveKit SDK.
"""
from __future__ import annotations

import os
import time
from typing import Any

from core.config import (
    FRAME_STALL_SECONDS, FRAME_STARTUP_GRACE_SECONDS, PUBLIC_ORIGIN)
from core.util import log, redact
from vendors.base import EntityHealth, Publisher

CAPTURE_WIDTH = int(os.environ.get("WARNER_CAPTURE_WIDTH", "2304"))
CAPTURE_HEIGHT = int(os.environ.get("WARNER_CAPTURE_HEIGHT", "1296"))


class ChromiumSupervisor:
    def __init__(self) -> None:
        self.playwright: Any = None
        self.browser: Any = None
        self.contexts: dict[str, Any] = {}
        self.pages: dict[str, Any] = {}
        self.status: dict[str, EntityHealth] = {}
        self.started_at: dict[str, float] = {}
        self.restarts: dict[str, int] = {}
        self.last_restart_at: dict[str, float] = {}

    async def start(self) -> None:
        if self.browser is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is required: pip install playwright && "
                "playwright install chromium") from exc
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            executable_path=os.environ.get("WARNER_CHROMIUM_EXECUTABLE") or None,
            args=["--autoplay-policy=no-user-gesture-required",
                  "--disable-dev-shm-usage", "--no-sandbox"],
        )
        log("headless Chromium started")

    async def _new_context(self) -> Any:
        # A separate context per camera: the SDK pauses decoding on
        # visibilitychange, so tabs sharing a context leave only the
        # active camera producing frames.
        context = await self.browser.new_context(
            viewport={"width": CAPTURE_WIDTH, "height": CAPTURE_HEIGHT})
        # Warner's WebCodecs path transfers the source canvas to an
        # OffscreenCanvas, which captureStream() then cannot see in
        # headless. Forcing the WASM decoder is the workaround, and it
        # goes away with the canvas.
        await context.add_init_script(
            'Object.defineProperty(globalThis, "VideoDecoder", '
            '{value: undefined, configurable: false, writable: false});')
        return context

    async def apply(self, plan: dict[str, list[Publisher]]) -> None:
        wanted = {cid for cid, pubs in plan.items()
                  if any(p.connected for p in pubs)}
        for camera_id in list(self.pages):
            if camera_id not in wanted:
                await self._teardown(camera_id)
                log(f"[{camera_id}] worker stopped")

        for camera_id in wanted:
            if camera_id in self.pages:
                self._note_applied(camera_id, plan[camera_id])
                continue
            await self._spawn(camera_id)
            self._note_applied(camera_id, plan[camera_id])

        await self._restart_stalled()

    def _note_applied(self, camera_id: str, publishers: list[Publisher]) -> None:
        """Echo back what this camera is actually running under, so the
        backend can compare intent against effect rather than trusting
        that a written row took."""
        health = self.status.get(camera_id)
        if not health or not publishers:
            return
        health.applied = {
            "desired_state": "connected",
            "rooms": [p.room for p in publishers if p.connected],
            "audio_capture_enabled": all(
                p.audio_capture_enabled for p in publishers if p.connected),
            "privacy_enabled": any(
                p.privacy_enabled for p in publishers if p.connected),
        }

    async def _spawn(self, camera_id: str) -> None:
        context = await self._new_context()
        page = await context.new_page()
        page.on("console", lambda msg, cid=camera_id:
                log(f"[{cid}] browser {msg.type}: {redact(msg.text)}"))
        page.on("pageerror", lambda exc, cid=camera_id:
                log(f"[{cid}] browser error: {exc}"))
        page.on("response", lambda response, cid=camera_id:
                log(f"[{cid}] browser HTTP {response.status}: {redact(response.url)}")
                if response.status >= 400 else None)
        await page.goto(f"{PUBLIC_ORIGIN}/runtime?camera_id={camera_id}",
                        wait_until="domcontentloaded")
        now = time.time()
        self.pages[camera_id] = page
        self.contexts[camera_id] = context
        self.started_at[camera_id] = now
        self.status[camera_id] = EntityHealth(
            updated_at=now,
            restarts=self.restarts.get(camera_id, 0),
            last_restart_at=self.last_restart_at.get(camera_id, 0.0))
        log(f"[{camera_id}] worker started")

    async def _teardown(self, camera_id: str) -> None:
        page = self.pages.pop(camera_id, None)
        context = self.contexts.pop(camera_id, None)
        self.started_at.pop(camera_id, None)
        for closeable, what in ((page, "page"), (context, "context")):
            if not closeable:
                continue
            try:
                await closeable.close()
            except Exception as exc:  # noqa: BLE001 — may already be disposed
                log(f"[{camera_id}] {what} close ignored: {exc}")
        self.status.pop(camera_id, None)
        self.restarts.pop(camera_id, None)
        self.last_restart_at.pop(camera_id, None)

    async def _restart_stalled(self) -> None:
        """A camera that stops sending frames never EOFs, so silence is
        the only signal that it has died."""
        now = time.time()
        for camera_id in list(self.pages):
            health = self.status.get(camera_id, EntityHealth())
            if health.last_frame_at:
                age = now - health.last_frame_at
                if age <= FRAME_STALL_SECONDS:
                    continue
                reason = (f"no video frame for {age:.1f}s "
                          f"(limit {FRAME_STALL_SECONDS:.0f}s)")
            else:
                age = now - self.started_at.get(camera_id, now)
                if age <= FRAME_STARTUP_GRACE_SECONDS:
                    continue
                reason = (f"no first video frame after {age:.1f}s "
                          f"(limit {FRAME_STARTUP_GRACE_SECONDS:.0f}s)")

            count = self.restarts.get(camera_id, 0) + 1
            log(f"[{camera_id}] VIDEO STALLED: {reason}; recreating (restart {count})")
            await self._teardown(camera_id)
            self.restarts[camera_id] = count
            self.last_restart_at[camera_id] = now
            self.status[camera_id] = EntityHealth(
                state="video_stalled", detail=reason, updated_at=now,
                restarts=count, last_restart_at=now)

    def report(self, camera_id: str, body: dict[str, Any]) -> None:
        now = time.time()
        health = self.status.get(camera_id) or EntityHealth()
        state = str(body.get("state", "unknown"))[:80]
        if state == "frame_heartbeat":
            health.last_frame_at = now
            try:
                health.frame_count = max(
                    health.frame_count, int(body.get("frame_count", 0)))
            except (TypeError, ValueError):
                pass
        else:
            health.state = state
            health.detail = str(body.get("detail", ""))[:500]
        health.updated_at = now
        self.status[camera_id] = health

    async def close(self) -> None:
        for camera_id in list(self.pages):
            await self._teardown(camera_id)
        for closeable, what in ((self.browser, "browser"),
                                (self.playwright, "playwright")):
            if not closeable:
                continue
            try:
                await (closeable.close() if what == "browser"
                       else closeable.stop())
            except Exception as exc:  # noqa: BLE001
                log(f"{what} shutdown ignored: {exc}")
        self.browser = None
        self.playwright = None
