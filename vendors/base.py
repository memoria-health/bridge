#!/usr/bin/env python3
"""The contract every camera integration implements.

The point of the split: which cameras publish, into which rooms, with
what privacy and audio state, is decided ONCE in `core/` against the
backend. A vendor only answers "how do I get frames out of this
particular hardware". Swapping Warner for the next brand must not reach
the app, the backend, or the fleet telemetry.

Shapes here mirror `nemara-backend/docs/design/fleet-service-contract.md`:
health is per ENTITY rather than per process, capabilities are declared
rather than assumed, and what was applied is echoed back so the backend
can compare intent against effect.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Publisher:
    """One LiveKit room a physical camera must appear in.

    A camera can be wanted by several rooms at once (a shared camera in a
    two-patient home), which is why this is a list per camera and why a
    vendor must fan one stream out rather than open one per room.
    """

    identity: str
    room: str
    connected: bool
    audio_capture_enabled: bool
    privacy_enabled: bool
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "room": self.room,
            "desired_state": "connected" if self.connected else "disconnected",
            "stream_reasons": list(self.reasons),
            "audio_capture_enabled": self.audio_capture_enabled,
            "privacy_enabled": self.privacy_enabled,
        }


@dataclass
class EntityHealth:
    """What a vendor says about ONE camera, every poll.

    `applied` is the load-bearing field: it is the difference between
    "the backend sent disconnect" and "the camera stopped publishing".
    """

    state: str = "starting"
    detail: str = ""
    updated_at: float = 0.0
    last_frame_at: float = 0.0
    frame_count: int = 0
    restarts: int = 0
    last_restart_at: float = 0.0
    applied: dict[str, Any] = field(default_factory=dict)

    def healthy(self, now: float, stall_seconds: float) -> bool:
        return bool(self.last_frame_at) and (now - self.last_frame_at) <= stall_seconds


class Vendor(ABC):
    """One camera brand. Everything brand-specific lives behind this."""

    name: str = "unknown"
    # Declared, never assumed. The backend and the app render an absent
    # capability as absent rather than as healthy.
    capabilities: tuple[str, ...] = ()

    # ── identity and inventory ────────────────────────────────────
    @abstractmethod
    def camera_id(self, device_key: str) -> str:
        """A stable id for the physical unit, derived from vendor identity."""

    @abstractmethod
    def entry_from_provision(
        self, body: dict[str, Any], identities: list[str], name: str,
    ) -> dict[str, Any]:
        """Build the stored inventory row from a provisioning request."""

    @abstractmethod
    def public_fields(self, entry: dict[str, Any]) -> dict[str, Any]:
        """The subset of an inventory row safe to return over the API.

        Credentials live in the same row; this is what keeps them from
        leaving by accident.
        """

    # ── lifecycle ─────────────────────────────────────────────────
    @abstractmethod
    async def start(self) -> None:
        """Bring up whatever the vendor needs once, before any camera."""

    @abstractmethod
    async def apply(self, plan: dict[str, list[Publisher]]) -> None:
        """Reconcile to `plan`: camera_id -> the rooms it must publish into.

        Called on a loop. Must be idempotent, and must stop a camera that
        is absent from the plan or whose publishers are all disconnected.
        """

    @abstractmethod
    def health(self) -> dict[str, EntityHealth]:
        """Per-camera health. One row per camera the vendor owns."""

    @abstractmethod
    async def close(self) -> None:
        """Release everything. Must tolerate being called twice."""

    def report(self, camera_id: str, body: dict[str, Any]) -> None:
        """Take a status update from an out-of-process worker.

        Vendors whose workers run inside the bridge can ignore this; ones
        that supervise a separate process need it to learn about frames.
        """

    # ── optional hooks ────────────────────────────────────────────
    def static_mounts(self) -> dict[str, Path]:
        """URL prefix -> directory, for vendors that need to serve assets."""
        return {}

    def add_routes(self, app: Any, bridge: Any) -> None:
        """Vendor-specific HTTP routes. Most vendors need none."""

    def preflight(self) -> None:
        """Fail loudly at startup if the vendor cannot possibly work."""
