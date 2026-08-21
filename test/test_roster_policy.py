"""The publish decision, which is the part that can hurt someone.

These lock in behaviour before the publish path moves into Python. Every
case here is a bug this bridge actually shipped: a disconnect that did
nothing, a bathroom camera visible to everyone, a microphone open at a
site that does not permit recording.
"""
from __future__ import annotations

import pytest

from core.roster import Roster

CAM = {"camera_id": "warner_abc", "livekit_identities": ["cam_1", "cam_2"]}


def roster(mode: str = "continuous", *items) -> Roster:
    r = Roster({"backend_url": "", "gateway_token": "", "poll_seconds": 3,
                "stream_mode": mode})
    r.items = list(items)
    return r


def item(identity="cam_1", room="patient_x", **kw) -> dict:
    return {"livekit_identity": identity, "livekit_room": room, **kw}


# ── the off switch ────────────────────────────────────────────────
def test_explicit_disconnect_wins_in_continuous_mode():
    """The bug: continuous mode hardcoded connected and never read
    desired_state, so pressing disconnect wrote a row and nothing else."""
    r = roster("continuous", item(desired_state="disconnected"))
    published = r.publishers_for(CAM)
    assert len(published) == 1
    assert published[0].connected is False
    assert published[0].reasons == ("backend_disconnected",)


def test_explicit_disconnect_wins_in_policy_mode_even_with_a_viewer():
    r = roster("policy", item(desired_state="disconnected", viewer_count=3,
                              ai_enabled=True))
    assert r.publishers_for(CAM)[0].connected is False


def test_continuous_mode_publishes_when_not_explicitly_off():
    r = roster("continuous", item(desired_state="connected"))
    assert r.publishers_for(CAM)[0].connected is True


# ── safe defaults for the two privacy flags ───────────────────────
def test_privacy_defaults_to_enabled_when_the_roster_omits_it():
    """An older backend that omits the field must err TOWARD privacy.
    Defaulting to False would expose a bathroom camera to every
    subscriber in the room."""
    r = roster("continuous", item(desired_state="connected"))
    assert r.publishers_for(CAM)[0].privacy_enabled is True


def test_audio_defaults_to_disabled_when_the_roster_omits_it():
    """The facilities audio kill. A missing field must not open a
    microphone at a site that does not permit recording."""
    r = roster("continuous", item(desired_state="connected"))
    assert r.publishers_for(CAM)[0].audio_capture_enabled is False


@pytest.mark.parametrize("flag", [True, False])
def test_flags_are_carried_through_verbatim(flag):
    r = roster("continuous", item(desired_state="connected",
                                  privacy_enabled=flag,
                                  audio_capture_enabled=flag))
    published = r.publishers_for(CAM)[0]
    assert published.privacy_enabled is flag
    assert published.audio_capture_enabled is flag


# ── policy mode ───────────────────────────────────────────────────
def test_ai_keeps_the_stream_alive_with_nobody_watching():
    r = roster("policy", item(ai_enabled=True))
    published = r.publishers_for(CAM)[0]
    assert published.connected is True
    assert "ai_analysis" in published.reasons


def test_paused_ai_and_no_viewer_means_no_stream():
    r = roster("policy", item(ai_enabled=True, ai_paused=True))
    assert r.publishers_for(CAM)[0].connected is False


def test_a_viewer_alone_is_enough():
    r = roster("policy", item(viewer_count=1))
    published = r.publishers_for(CAM)[0]
    assert published.connected is True
    assert "mobile_viewer" in published.reasons


@pytest.mark.parametrize("value", ["true", "YES", "on", "1", "enabled", True])
def test_truthy_strings_from_the_wire_are_accepted(value):
    """The roster is JSON from another service; booleans arrive as
    strings often enough that treating "true" as False would silently
    stop analysis."""
    r = roster("policy", item(ai_enabled=value))
    assert r.publishers_for(CAM)[0].connected is True


@pytest.mark.parametrize("value", ["false", "off", "0", "", None, False])
def test_falsy_values_do_not_start_analysis(value):
    r = roster("policy", item(ai_enabled=value))
    assert r.publishers_for(CAM)[0].connected is False


def test_a_garbage_viewer_count_does_not_crash_the_poll():
    r = roster("policy", item(viewer_count="not-a-number", ai_enabled=True))
    assert r.publishers_for(CAM)[0].connected is True


# ── scoping ───────────────────────────────────────────────────────
def test_only_this_camera_s_identities_are_returned():
    """One bridge holds several cameras. Returning another camera's
    publisher would make it publish someone else's room."""
    r = roster("continuous",
               item("cam_1", "patient_a", desired_state="connected"),
               item("cam_99", "patient_z", desired_state="connected"))
    published = r.publishers_for(CAM)
    assert [p.identity for p in published] == ["cam_1"]


def test_one_camera_fans_out_to_every_room_that_wants_it():
    """A shared camera in a two-patient home. It must be ONE capture
    published into both rooms, never two captures."""
    r = roster("continuous",
               item("cam_1", "patient_a", desired_state="connected"),
               item("cam_2", "patient_b", desired_state="connected"))
    published = r.publishers_for(CAM)
    assert sorted(p.room for p in published) == ["patient_a", "patient_b"]


def test_plan_is_keyed_by_camera():
    r = roster("continuous", item("cam_1", desired_state="connected"))
    plan = r.plan([CAM])
    assert list(plan) == ["warner_abc"]
    assert plan["warner_abc"][0].identity == "cam_1"


def test_an_empty_roster_yields_no_publishers():
    assert roster("continuous").publishers_for(CAM) == []


# ── wire shape ────────────────────────────────────────────────────
def test_to_json_uses_the_vocabulary_the_runtime_page_expects():
    r = roster("continuous", item(desired_state="connected",
                                  privacy_enabled=True))
    body = r.publishers_for(CAM)[0].to_json()
    assert body["desired_state"] == "connected"
    assert body["identity"] == "cam_1"
    assert body["room"] == "patient_x"
    assert body["privacy_enabled"] is True
    assert body["audio_capture_enabled"] is False
    assert isinstance(body["stream_reasons"], list)
