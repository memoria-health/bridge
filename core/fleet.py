#!/usr/bin/env python3
"""Telemetry as a PRECONDITION of instruction.

`nemara-backend/docs/design/fleet-service-contract.md`, rule 2. The poll
is a POST whose body carries this service's health and the state of
every camera it owns, and a poll without a report is refused. That makes
telemetry loss impossible rather than merely discouraged: a service that
stops reporting stops being told what to do, and goes visibly stale
within one poll interval.

Written because the opposite happened. The bridge polled for
`desired_state`, `privacy_enabled` and `audio_capture_enabled`, acted on
none of them, and reported nothing back — and nothing noticed for weeks,
because reporting was optional and acting was unverified.

Rule 3 is the shape: one row per CAMERA, never one row for the process.
One bridge polls for many cameras, so "I am alive" is not a statement
about the things it is responsible for.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from vendors.base import EntityHealth

CONTRACT = "fleet-v1"


def report_body(
    *,
    instance_id: str,
    kind: str,
    version: str,
    capabilities: Iterable[str],
    entities: Mapping[str, EntityHealth],
    now: float,
    stall_seconds: float,
) -> dict[str, Any]:
    """The poll body. Carries no credential: it reaches the backend on
    every poll and lands in its logs."""
    return {
        "contract": CONTRACT,
        "service": {
            "kind": kind,
            "instance_id": instance_id,
            "version": version,
            # Declared, never assumed. A capability that is absent must
            # render as absent downstream, not as healthy.
            "capabilities": list(capabilities),
        },
        "entities": [
            _entity(camera_id, health, now, stall_seconds)
            for camera_id, health in sorted(entities.items())
        ],
    }


def _entity(camera_id: str, health: EntityHealth,
            now: float, stall_seconds: float) -> dict[str, Any]:
    # None rather than a number when no frame has ever arrived. Zero
    # would read as "a frame just landed", which is the opposite.
    age = (round(max(0.0, now - health.last_frame_at), 1)
           if health.last_frame_at else None)
    return {
        "id": camera_id,
        "state": health.state,
        "healthy": health.healthy(now, stall_seconds),
        "detail": health.detail,
        "last_frame_age_s": age,
        "frame_count": health.frame_count,
        "restarts": health.restarts,
        # What we did with the LAST instruction, so the backend can
        # compare intent against effect rather than trusting that a
        # written row took.
        "applied": dict(health.applied),
    }
