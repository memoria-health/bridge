#!/usr/bin/env python3
"""Camera bridge: any vendor's cameras into LiveKit as `cam_*` participants.

Replaces `warner_bridge.py`, which fused the Warner SDK, the backend
roster, LiveKit tokens, auth and Chromium supervision into one file.
The split is `core/` (identical for every brand) and `vendors/<name>/`
(how to get frames out of one particular camera).

Run:  python bridge.py          # BRIDGE_VENDOR selects the integration
"""
from __future__ import annotations

import asyncio
import signal

from aiohttp import web

from core.config import (
    API_KEY, HOST, PORT, TRUST_LOOPBACK, VENDOR, load_settings,
    validate_settings)
from core.server import Bridge, create_app
from core.util import log
from vendors import build


async def main() -> None:
    settings = load_settings()
    validate_settings(settings)
    vendor = build(VENDOR)
    vendor.preflight()

    bridge = Bridge(settings, vendor)
    app = await create_app(bridge)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    log(f"{vendor.name} bridge on http://{HOST}:{PORT}/")
    if not TRUST_LOOPBACK and not API_KEY:
        # Fail CLOSED and say so. Silently serving the provisioning page
        # to whoever can reach the port is what this replaces.
        log("BRIDGE_KEY is unset and loopback is not trusted, so the admin "
            "page and provisioning are closed to everyone. Cameras already "
            "provisioned keep publishing.")
    # Before polling, so a gateways row created as a single-tenant box —
    # by a bridge that never said what it was — is repaired before the
    # roster it scopes is asked for.
    await bridge.enroll_stored()

    tasks = [asyncio.create_task(bridge.roster.run()),
             asyncio.create_task(bridge.run_vendor())]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await vendor.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
