# Ubuntu Linux deployment

Read `STREAMING_ARCHITECTURE.md` and `TECHNICAL_SOP.md` before production use.
Ubuntu Server 22.04/24.04 is the target environment.

For the complete step-by-step installation procedure, see
`LINUX_INSTALLATION.md`.

Copy this entire directory to `/opt/memoria-warner-bridge`, then run:

```bash
sudo chown -R root:root /opt/memoria-warner-bridge
cd /opt/memoria-warner-bridge
sudo bash scripts/install.sh
sudo nano cameras.json
sudo chown memoria:memoria cameras.json
sudo chmod 600 cameras.json
sudo systemctl enable --now memoria-warner-bridge
sudo systemctl status memoria-warner-bridge
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
sudo journalctl -u memoria-warner-bridge -f
sudo systemctl restart memoria-warner-bridge
```

This build includes automatic Warner video-freeze recovery. The browser runtime
reports a frame heartbeat every five seconds. If no frame arrives for 20
seconds, the bridge recreates only the affected camera worker. Inspect
`frame_age_seconds` and `restart_count` in `/api/health`, and search the journal
for `VIDEO STALLED` when investigating recovery events.

Optional environment overrides are `WARNER_FRAME_STALL_SECONDS` (default 20)
and `WARNER_FRAME_STARTUP_GRACE_SECONDS` (default 45).

Allow inbound TCP 8090 only from the administrator LAN/VPN. Never expose it to
the public Internet.
