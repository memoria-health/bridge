#!/usr/bin/env python3
"""The camera inventory: which physical units this bridge owns.

Rows hold vendor credentials, so nothing here is returned over the API
directly — `Vendor.public_fields` decides what may leave.
"""
from __future__ import annotations

from typing import Any

from core.config import INVENTORY_PATH
from core.util import load_json, write_json_atomic


def read_inventory() -> dict[str, Any]:
    return load_json(INVENTORY_PATH, {"version": 1, "cameras": []})


def write_inventory(cfg: dict[str, Any]) -> None:
    write_json_atomic(INVENTORY_PATH, cfg)


def cameras() -> list[dict[str, Any]]:
    return read_inventory().get("cameras", [])


def find(camera_id: str) -> dict[str, Any] | None:
    return next((c for c in cameras() if c.get("camera_id") == camera_id), None)


def add(entry: dict[str, Any]) -> None:
    cfg = read_inventory()
    cfg.setdefault("version", 1)
    cfg.setdefault("cameras", []).append(entry)
    write_inventory(cfg)
