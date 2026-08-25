"""A vendor that fails to start must be retried, not abandoned.

`run_vendor` called `vendor.start()` once, ABOVE its try, so any failure
killed the task outright. Nothing retried it and nothing reported it:
the bridge went on answering /api/health, polling the roster, listing
its cameras — and publishing nothing, forever. It reads as a healthy
bridge with a quiet house.

That is not hypothetical. `vendor.start()` launches Chromium, and
Playwright raises `Executable doesn't exist` when PLAYWRIGHT_BROWSERS_PATH
is unset — which `scripts/install.sh` sets and a hand-started process
does not (2026-08-23). The causes are all transient or fixable in place:
a browser still installing, a machine still booting, a path set a minute
later. So retry, and log every failure.
"""
from __future__ import annotations

import asyncio

import pytest

from core import store
from core.server import Bridge
from vendors.warner import WarnerVendor


class _Vendor(WarnerVendor):
    """Fails `start()` a fixed number of times, then succeeds."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.remaining = failures
        self.starts = 0
        self.applies = 0

    async def start(self) -> None:
        self.starts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("Executable doesn't exist at .../chrome-headless-shell")

    async def apply(self, plan) -> None:  # noqa: ANN001
        self.applies += 1

    def health(self):
        return {}


def _bridge(monkeypatch, tmp_path, vendor):
    monkeypatch.setattr(store, "INVENTORY_PATH", tmp_path / "inventory.json")
    return Bridge(
        {"backend_url": "https://backend", "gateway_token": "t",
         "instance_id": "bridge-test", "livekit_url": "wss://lk",
         "poll_seconds": 3, "stream_mode": "continuous"},
        vendor)


async def _run_passes(bridge, monkeypatch, passes: int) -> None:
    """Drive `run_vendor` for an exact number of loop passes.

    The loop sleeps 2s between passes, so waiting in wall-clock would
    make these tests slow AND flaky. Counting the sleeps instead makes
    "it retried" an exact assertion rather than a race with a timer.
    """
    real_sleep = asyncio.sleep
    seen = {"n": 0}

    async def counted(_secs, *a, **k):
        seen["n"] += 1
        if seen["n"] >= passes:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", counted)
    try:
        await bridge.run_vendor()
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_a_failed_start_is_retried_and_then_publishes(monkeypatch, tmp_path):
    """THE regression: one bad start used to end the task permanently."""
    vendor = _Vendor(failures=2)
    bridge = _bridge(monkeypatch, tmp_path, vendor)

    await _run_passes(bridge, monkeypatch, passes=4)

    assert vendor.starts >= 3, "gave up instead of retrying start()"
    assert vendor.applies > 0, (
        "never reconciled — the bridge would poll and look healthy while "
        "publishing nothing"
    )


@pytest.mark.asyncio
async def test_start_is_not_repeated_once_it_succeeds(monkeypatch, tmp_path):
    """Chromium is expensive; re-launching it every 2s would be its own
    outage."""
    vendor = _Vendor(failures=0)
    bridge = _bridge(monkeypatch, tmp_path, vendor)

    await _run_passes(bridge, monkeypatch, passes=3)

    assert vendor.starts == 1
    assert vendor.applies > 1, "the reconcile loop should keep running"


@pytest.mark.asyncio
async def test_a_failing_apply_does_not_end_the_loop(monkeypatch, tmp_path):
    """Unchanged behaviour, pinned: one bad reconcile pass is not fatal."""
    vendor = _Vendor(failures=0)
    calls = {"n": 0}

    async def _boom(plan):  # noqa: ANN001
        calls["n"] += 1
        raise RuntimeError("page closed")

    monkeypatch.setattr(vendor, "apply", _boom)
    bridge = _bridge(monkeypatch, tmp_path, vendor)

    await _run_passes(bridge, monkeypatch, passes=3)

    assert calls["n"] > 1
