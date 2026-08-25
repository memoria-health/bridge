"""The health check has to fail when the bridge is silently useless.

It read `data["workers"]`, which /api/health has never returned in this
shape — the body carries `entities`, a LIST. `.get("workers", {})`
yielded {} and the loop never ran, so the only surviving assertion was
`ok`: whether the last roster poll succeeded. A bridge whose Chromium
never started, or whose every camera had stalled, polls the roster
perfectly well and exited 0.

README points operators at this script. An on-call engineer trusting its
exit code during an incident was being told everything was fine while
every camera was dark — the same "answers, polls, publishes nothing"
failure the service keeps producing, reproduced in the tool meant to
catch it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from health_check import verdict  # noqa: E402


def _entity(**kw):
    base = {"id": "c51733e6-0cc7-4f0d-a785-51747905eb0b",
            "state": "camera_connected", "healthy": True,
            "detail": "2304x1296 H265", "last_frame_age_s": 1.2}
    base.update(kw)
    return base


def test_a_healthy_bridge_passes():
    code, _ = verdict({"ok": True, "configured_cameras": 1,
                       "entities": [_entity()]})
    assert code == 0


def test_configured_cameras_but_no_entities_fails():
    """THE regression. This is the missing-Chromium shape: roster fine,
    cameras configured, vendor never started, everything else green."""
    code, msg = verdict({"ok": True, "configured_cameras": 1, "entities": []})
    assert code == 5, "a bridge publishing nothing must not exit 0"
    assert "never started" in msg


def test_an_unhealthy_camera_fails():
    code, _ = verdict({"ok": True, "configured_cameras": 1,
                       "entities": [_entity(healthy=False, state="page_dead")]})
    assert code == 3


def test_stale_frames_fail():
    code, msg = verdict({"ok": True, "configured_cameras": 1,
                         "entities": [_entity(last_frame_age_s=45.0)]},
                        stall_seconds=20.0)
    assert code == 4
    assert "stale" in msg


def test_a_camera_that_has_never_produced_a_frame_fails():
    """`None` is not zero. A camera that has never delivered a frame is
    not fresh — reading a missing age as 0 would call it healthy."""
    code, _ = verdict({"ok": True, "configured_cameras": 1,
                       "entities": [_entity(last_frame_age_s=None)]})
    assert code == 4


def test_a_bridge_that_says_not_ok_fails():
    code, _ = verdict({"ok": False, "configured_cameras": 0, "entities": []})
    assert code == 2


def test_no_cameras_configured_is_not_an_error():
    """A freshly installed bridge with nothing provisioned yet is idle,
    not broken — paging on it would train operators to ignore this."""
    code, _ = verdict({"ok": True, "configured_cameras": 0, "entities": []})
    assert code == 0
