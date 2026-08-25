"""This bridge's own bearer token: minted once, never shared, never logged."""
from __future__ import annotations

import json
import stat

from core.identity import fingerprint, load_identity


def test_minting_is_once_and_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    path = tmp_path / "identity.json"
    first = load_identity(path)
    second = load_identity(path)
    assert first["source"] == "minted"
    assert second["source"] == "file"
    assert first["token"] == second["token"]
    assert first["instance_id"] == second["instance_id"]


def test_the_token_file_is_not_world_readable(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    path = tmp_path / "identity.json"
    load_identity(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"identity file is {oct(mode)}, must be 0600"


def test_two_bridges_never_share_a_token(tmp_path, monkeypatch):
    """The whole point. One leak must not be a fleet-wide leak."""
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    a = load_identity(tmp_path / "a.json")
    b = load_identity(tmp_path / "b.json")
    assert a["token"] != b["token"]
    assert a["instance_id"] != b["instance_id"]


def test_token_has_the_same_strength_as_the_backend_minter(tmp_path, monkeypatch):
    """The backend mints 32 bytes url-safe, which is 43 chars. A shorter
    token here would be a weaker credential for the same privilege."""
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    assert len(load_identity(tmp_path / "id.json")["token"]) == 43


def test_env_override_is_used_and_never_written_to_disk(tmp_path, monkeypatch):
    """An ops override must leave nothing behind to go stale and later
    be picked up as if it were this instance's own identity."""
    monkeypatch.setenv("BRIDGE_TOKEN", "ops-supplied-token")
    path = tmp_path / "identity.json"
    identity = load_identity(path)
    assert identity["token"] == "ops-supplied-token"
    assert identity["source"] == "env"
    assert not path.exists()


def test_a_file_missing_its_token_is_re_minted(tmp_path, monkeypatch):
    """A truncated or hand-edited file must not leave the bridge with no
    credential at all."""
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"instance_id": "bridge-old"}))
    identity = load_identity(path)
    assert identity["token"]
    assert identity["source"] == "minted"


def test_fingerprint_hides_the_middle_of_the_token():
    """Fingerprints go in logs. They must identify a token against a
    dashboard row without being usable."""
    token = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg"
    printed = fingerprint(token)
    assert token not in printed
    assert "GHIJKLMNOPQRSTUVWXYZ" not in printed
    assert printed.startswith("ABCDEF")
    assert "defg" in printed
    assert "43" in printed


def test_fingerprint_of_nothing_is_not_an_exception():
    assert fingerprint("") == "<none>"
