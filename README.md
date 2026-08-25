# Ubuntu Linux deployment

Read `STREAMING_ARCHITECTURE.md` and `TECHNICAL_SOP.md` before production use.
Ubuntu Server 22.04/24.04 is the target environment.

For a hosted container instead of a box, see `CLOUD_DEPLOYMENT.md` — same
program, same environment, but the platform names the port and wipes the
filesystem, so it needs a volume and one replica. The bridge never touches
the camera's LAN, which is what makes that possible.

For the complete step-by-step installation procedure, see
`LINUX_INSTALLATION.md`.

Copy this entire directory to `/opt/memoria-bridge`, then run:

```bash
sudo chown -R root:root /opt/memoria-bridge
cd /opt/memoria-bridge
sudo bash scripts/install.sh
sudo nano cameras.json
sudo chown memoria:memoria cameras.json
sudo chmod 600 cameras.json
sudo systemctl enable --now memoria-bridge
sudo systemctl status memoria-bridge
```

## Do not start it by hand

Always `systemctl`, never `python bridge.py` from a shell. The unit sets
`PLAYWRIGHT_BROWSERS_PATH` to the vendored `browsers/` directory, and
without it Playwright looks in `~/.cache/ms-playwright`, finds nothing,
and `vendor.start()` raises `Executable doesn't exist`.

That failure is invisible from outside. The bridge still answers
`/api/health`, still polls the roster, still lists its cameras — and
publishes nothing. Check `entities` in `/api/health`: an empty array
while cameras are configured means the vendor never started.

Useful commands:

```bash
sudo bash scripts/health.sh
sudo bash scripts/debug.sh
sudo journalctl -u memoria-bridge -f
sudo systemctl restart memoria-bridge
```

This build includes automatic Warner video-freeze recovery. The browser runtime
reports a frame heartbeat every five seconds. If no frame arrives for 20
seconds, the bridge recreates only the affected camera worker. Inspect
`last_frame_age_s` and `restarts` on each entry of the `entities` array in
`/api/health`, and search the journal for `VIDEO STALLED` when investigating
recovery events. (These were `frame_age_seconds` / `restart_count` under a
`workers` map; the body carries a list of `entities` now, keyed by the
backend's camera uuid.)

Optional environment overrides are `WARNER_FRAME_STALL_SECONDS` (default 20)
and `WARNER_FRAME_STARTUP_GRACE_SECONDS` (default 45).

Allow inbound TCP 8090 only from the administrator LAN/VPN. Never expose it to
the public Internet.
