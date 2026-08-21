#!/usr/bin/env python3
"""Vendor registry. Adding a camera brand means adding one line here."""
from __future__ import annotations

from vendors.base import EntityHealth, Publisher, Vendor

__all__ = ["EntityHealth", "Publisher", "Vendor", "build"]


def build(name: str) -> Vendor:
    if name == "warner":
        from vendors.warner import WarnerVendor
        return WarnerVendor()
    raise RuntimeError(f"unknown vendor {name!r}; set BRIDGE_VENDOR")
