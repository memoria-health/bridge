"""The report a poll must carry to be answered.

Rules 2, 3 and 4 of docs/design/fleet-service-contract.md, which exist
because this bridge polled for instructions, acted on none of them, and
reported nothing — and nothing detected that for weeks, since reporting
was optional and acting was unverified.
"""
from __future__ import annotations

from core.fleet import report_body
from vendors.base import EntityHealth

NOW = 1_000.0
STALL = 20.0


def health(**kw) -> EntityHealth:
    base = dict(state="publishing", last_frame_at=NOW - 1, frame_count=42,
                publishing=True, last_publish_at=NOW - 1,
                applied={"desired_state": "connected", "room": "patient_a",
                         "privacy_enabled": True, "audio_capture_enabled": False})
    base.update(kw)
    return EntityHealth(**base)


def body(entities=None, **kw):
    return report_body(
        instance_id=kw.get("instance_id", "bridge-0ea7166c"),
        kind=kw.get("kind", "camera-bridge"),
        version=kw.get("version", "2.0.0"),
        capabilities=kw.get("capabilities", ("privacy_sever", "audio_kill")),
        entities=entities if entities is not None else {"warner_a": health()},
        now=NOW, stall_seconds=STALL)


# ── rule 3: per entity, never per process ─────────────────────────
def test_every_owned_camera_gets_a_row():
    """The structural bug: one process polled for N cameras, so
    last_heartbeat_at was stamped for ALL of them while the bridge
    lived — including a camera whose worker died ten minutes ago."""
    report = body({"warner_a": health(), "warner_b": health()})
    assert {e["id"] for e in report["entities"]} == {"warner_a", "warner_b"}


def test_a_healthy_camera_reports_healthy():
    """Guards the assertions below from passing for the wrong reason."""
    assert body()["entities"][0]["healthy"] is True


def test_a_stalled_camera_reports_unhealthy_while_the_bridge_is_fine():
    report = body({"warner_a": health(last_frame_at=NOW - 300)})
    assert report["entities"][0]["healthy"] is False


def test_frames_arriving_is_not_enough_to_call_a_camera_healthy():
    """The page can be happily receiving frames from the camera while
    the decode, the frame socket, the LiveKit room or the pump is dead.
    That was the right measurement when the page also published; it
    stopped being so the moment publishing moved to Python."""
    report = body({"warner_a": health(publishing=False, last_publish_at=0.0)})
    assert report["entities"][0]["healthy"] is False


def test_a_camera_whose_frames_stopped_reaching_livekit_is_unhealthy():
    report = body({"warner_a": health(last_publish_at=NOW - 300)})
    assert report["entities"][0]["healthy"] is False


def test_a_camera_that_never_sent_a_frame_is_not_healthy():
    """No frame yet is not the same as fine. A blind camera looks
    exactly like a quiet house."""
    report = body({"warner_a": health(last_frame_at=0.0)})
    assert report["entities"][0]["healthy"] is False
    assert report["entities"][0]["last_frame_age_s"] is None


def test_frame_age_is_reported_so_the_backend_can_judge_for_itself():
    report = body({"warner_a": health(last_frame_at=NOW - 4.5)})
    assert report["entities"][0]["last_frame_age_s"] == 4.5


def test_owning_no_cameras_is_still_a_valid_report():
    """A freshly enrolled bridge owns nothing. It must still be able to
    poll, or it can never be told about its first camera."""
    assert body({})["entities"] == []


# ── rule 2: what was APPLIED, echoed back ─────────────────────────
def test_the_report_says_what_actually_took_effect():
    """The difference between "we sent disconnect" and "the camera
    stopped publishing". A caregiver pressing disconnect wrote a
    database row for months while the camera kept streaming, and every
    dashboard showed the row."""
    applied = body()["entities"][0]["applied"]
    assert applied["desired_state"] == "connected"
    assert applied["privacy_enabled"] is True
    assert applied["audio_capture_enabled"] is False


def test_a_camera_with_nothing_applied_yet_says_so_rather_than_guessing():
    report = body({"warner_a": health(applied={})})
    assert report["entities"][0]["applied"] == {}


# ── rule 4: capabilities declared, never assumed ──────────────────
def test_capabilities_are_declared_by_the_service():
    assert body()["service"]["capabilities"] == ["privacy_sever", "audio_kill"]


def test_a_capability_we_do_not_implement_is_simply_absent():
    """Absent must render as absent, not as healthy. The camera
    advertises two-way talk; the bridge implements none of it."""
    assert "talk" not in body()["service"]["capabilities"]


# ── identity and versioning ───────────────────────────────────────
def test_the_report_identifies_this_instance():
    """Per-instance credentials are worthless for diagnosis if every
    bridge reports as "a bridge"."""
    assert body()["service"]["instance_id"] == "bridge-0ea7166c"
    assert body()["service"]["kind"] == "camera-bridge"


def test_the_contract_version_rides_along():
    """A consumer that would misread a newer body must be able to tell
    rather than silently misparse it."""
    assert body()["contract"] == "fleet-v1"


def test_no_credential_ever_appears_in_the_report():
    """This body goes to the backend on every poll and into its logs."""
    flat = repr(body({"warner_a": health()}))
    for forbidden in ("token", "secret", "password", "device_key"):
        assert forbidden not in flat.lower()
