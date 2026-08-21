#!/usr/bin/env python3
"""What the backend wants, translated into a per-camera publish plan.

This is the whole reason `core/` exists. Deciding which camera publishes
into which room, with which privacy and audio state, is identical for
every camera brand — so it is decided once, here, and a vendor is only
told the result.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from core.util import get_json, log
from vendors.base import Publisher


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {
            "1", "true", "yes", "on", "active", "enabled"}
    return bool(value)


class Roster:
    """Polls `/v1/gateway/roster` and caches the answer.

    The poll IS the heartbeat: a bridge that stops working stops polling,
    and there is no second mechanism to disagree with.
    """

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.items: list[dict[str, Any]] = []
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
                self.items = await asyncio.to_thread(self.fetch)
                self.error = None
                self.updated_at = time.time()
            except Exception as exc:  # noqa: BLE001 — a bad poll must not end the loop
                # Log a CHANGE of error, not every occurrence. A bridge
                # that has minted a token but not yet provisioned a
                # camera 401s on every poll by design, and three lines a
                # second of that buries whatever else goes wrong.
                if str(exc) != self.error:
                    log(f"roster poll failed: {exc}")
                self.error = str(exc)
            await asyncio.sleep(self.settings["poll_seconds"])

    @staticmethod
    def _policy(item: dict[str, Any]) -> tuple[bool, list[str]]:
        """Who still needs this stream. AI keeps it alive independently of
        anyone watching on a phone."""
        reasons: list[str] = []
        if str(item.get("desired_state", "")).lower() == "connected":
            reasons.append("backend_desired")
        try:
            if int(item.get("viewer_count", 0) or 0) > 0:
                reasons.append("mobile_viewer")
        except (TypeError, ValueError):
            pass
        if _truthy(item.get("mobile_active", False)):
            reasons.append("mobile_viewer")
        ai_enabled = _truthy(item.get("ai_enabled", item.get("analysis_enabled", False)))
        ai_paused = _truthy(item.get("ai_paused", item.get("analysis_paused", False)))
        if ai_enabled and not ai_paused:
            reasons.append("ai_analysis")
        return bool(reasons), sorted(set(reasons))

    def publisher_for(self, camera: dict[str, Any]) -> Publisher | None:
        """The ONE room this camera publishes into, or None.

        One physical camera is one row, one identity, one room (backend,
        2026-08-19). Provisioning used to loop over every patient, so a
        shared camera became N rows publishing N identical streams — N
        times the ingest and N times the model spend. Multi-patient is
        handled on the SUBSCRIBER side now: the camera streams to the
        primary's room and the other covered patients receive the
        results, never a second copy of the video.
        """
        identity = camera.get("livekit_identity")
        if not identity:
            return None
        for item in self.items:
            if item.get("livekit_identity") != identity:
                continue
            # An EXPLICIT disconnect wins in every mode. Continuous mode
            # used to hardcode connected and never read desired_state, so
            # a caregiver pressing disconnect changed a database row and
            # nothing else. With bathroom/bedroom privacy in the product,
            # a camera that ignores "off" is a privacy failure, not a
            # missing feature. Continuous means "keep publishing when
            # nobody is watching" — it never meant overriding the switch.
            if str(item.get("desired_state", "")).lower() == "disconnected":
                connected, reasons = False, ["backend_disconnected"]
            elif self.settings["stream_mode"] == "continuous":
                connected, reasons = True, ["continuous_bridge"]
            else:
                connected, reasons = self._policy(item)
            return Publisher(
                identity=item["livekit_identity"],
                room=item["livekit_room"],
                connected=connected,
                reasons=tuple(reasons),
                # Defaults are the SAFE ones, so an older backend that
                # omits a field errs toward privacy rather than away.
                # audio_capture_enabled=false is the facilities audio
                # kill; privacy_enabled marks a bathroom/bedroom camera.
                audio_capture_enabled=bool(item.get("audio_capture_enabled", False)),
                privacy_enabled=bool(item.get("privacy_enabled", True)),
            )
        return None

    def plan(self, cameras: list[dict[str, Any]]) -> dict[str, Publisher | None]:
        """camera_id -> the room it publishes into, or None if no longer
        on the roster (deleted, or unassigned so it must not publish)."""
        return {c["camera_id"]: self.publisher_for(c) for c in cameras}
