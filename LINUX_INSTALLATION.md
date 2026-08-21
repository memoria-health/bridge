# Memoria Warner Bridge — Linux Installation Procedure

This procedure installs the final Memoria Warner Bridge on Ubuntu Server 22.04
or 24.04. The bridge includes its own `aiohttp` web server. Do not run a
separate `python -m http.server` process.

## 1. Copy the package from Windows

From Windows PowerShell:

```powershell
scp "C:\Users\sysadministrator\Documents\Nemara-new-03-08-2026\release\memoria-warner-linux-final.zip" administrator@192.168.80.83:/home/administrator/
```

Replace the username and IP address when installing on another Linux machine.

## 2. Connect to Linux and extract

```powershell
ssh administrator@192.168.80.83
```

On Linux:

```bash
sudo apt-get update
sudo apt-get install -y unzip
cd /home/administrator
unzip memoria-warner-linux-final.zip
```

## 3. Copy the application to `/opt`

```bash
sudo mkdir -p /opt/memoria-bridge
sudo cp -a /home/administrator/memoria-warner-linux/. \
  /opt/memoria-bridge/
ls -la /opt/memoria-bridge
```

## 4. Run the installer

```bash
cd /opt/memoria-bridge
sudo bash scripts/install.sh
```

The installer:

- creates the `memoria` service user when needed;
- installs Python, pip, venv, curl and CA certificates;
- creates `/opt/memoria-bridge/.venv`;
- installs `aiohttp`, the LiveKit API SDK and Playwright;
- installs headless Chromium and its Linux dependencies;
- creates `cameras.json` from the example when it is missing;
- installs the systemd service definition.

## 5. Configure the bridge

```bash
sudo nano /opt/memoria-bridge/cameras.json
```

Enter the production values:

```json
{
  "backend_url": "https://YOUR-NEMARA-BACKEND",
  "gateway_token": "YOUR_GATEWAY_TOKEN",
  "livekit_url": "wss://YOUR-PROJECT.livekit.cloud",
  "livekit_api_key": "YOUR_LIVEKIT_API_KEY",
  "livekit_api_secret": "YOUR_LIVEKIT_API_SECRET",
  "poll_seconds": 3,
  "stream_mode": "continuous"
}
```

The bridge adds `/v1/gateway/roster` itself. Do not include that path in
`backend_url`.

Save in Nano with `Ctrl+O`, Enter, then `Ctrl+X`.

## 6. Protect the configuration

```bash
sudo chown memoria:memoria \
  /opt/memoria-bridge/cameras.json
sudo chmod 600 \
  /opt/memoria-bridge/cameras.json
```

## 7. Start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now memoria-bridge
sudo systemctl status memoria-bridge
```

Expected status:

```text
Active: active (running)
```

Press `q` to exit the status display.

## 8. Verify bridge health

```bash
curl -s http://127.0.0.1:8090/api/health |
python3 -m json.tool
```

Before provisioning a camera, expected fields include:

```json
{
  "ok": true,
  "configured_cameras": 0,
  "stream_mode": "continuous",
  "roster_error": null
}
```

If `roster_error` contains `401` or `403`, verify the gateway token. If it
contains a timeout, verify the backend URL, DNS, firewall and Internet access.

## 9. Open the provisioning interface

From a computer on the administrator network, open:

```text
http://LINUX-SERVER-IP:8090/
```

Example:

```text
http://192.168.80.83:8090/
```

The systemd service already supplies this web server.

## 10. Provision a Warner camera

1. Sign in to the Warner account inside the provisioning page.
2. Load the device list.
3. Select the camera.
4. Enter the caregiver email and camera name.
5. For a normal single-channel camera, select:

```text
Channel: 0
Stream: Sub stream (0)
```

6. Click **Provision selected Warner camera**.

`channel=1` means the second physical channel and should only be used for a
genuine multi-channel recorder/NVR. The confirmed single-camera input is
`channel=0`, `stream_id=0`.

## 11. Verify continuous publication

```bash
curl -s http://127.0.0.1:8090/api/health |
python3 -m json.tool
```

For each mapped identity, expect:

```json
{
  "desired_state": "connected",
  "stream_reasons": ["continuous_bridge"]
}
```

The camera worker should report:

```json
"state": "publishing"
```

Check incoming camera frames:

```bash
sudo journalctl -u memoria-bridge \
  --since "5 minutes ago" --no-pager |
grep 'warner video frames'
```

Example:

```text
warner video frames: 1 800x448 H265
warner video frames: 250 800x448 H265
```

## 12. Logs and diagnostics

Follow logs:

```bash
sudo journalctl -u memoria-bridge -f
```

Show recent logs:

```bash
sudo journalctl -u memoria-bridge -n 200 --no-pager
```

Filter important events:

```bash
sudo journalctl -u memoria-bridge \
  --since "10 minutes ago" --no-pager |
grep -E 'worker|login succeeded|Open stream|warner video frames|LiveKit|publishing|HTTP 404|Aborted|error'
```

Capture the hidden Warner canvas locally:

```bash
```

Per-camera health, including frame age and what was applied, is on
`/api/health` for an authorized caller.

## 13. Service management

Restart:

```bash
sudo systemctl restart memoria-bridge
```

Stop:

```bash
sudo systemctl stop memoria-bridge
```

Start:

```bash
sudo systemctl start memoria-bridge
```

Disable automatic startup:

```bash
sudo systemctl disable --now memoria-bridge
```

Only one bridge instance should run. Do not manually start `bridge.py`
or `python -m http.server` while the systemd service is active.

## 13b. ffmpeg

ffmpeg decodes every camera. `scripts/install.sh` installs it, but if you
are setting a box up by hand, install it explicitly:

```bash
sudo apt-get install -y ffmpeg
```

The bridge refuses to start without it rather than failing per camera,
because a box that runs no cameras while looking healthy is the worst
outcome. It did not need ffmpeg before 2026-08-21, when the browser
still decoded in WebAssembly.

## 14. Back up protected data

```bash
sudo cp /opt/memoria-bridge/cameras.json \
  /home/administrator/cameras.json.backup
sudo cp /opt/memoria-bridge/inventory.json \
  /home/administrator/inventory.json.backup
```

Both backups contain credentials and must be stored securely.

## 15. Upgrade an existing installation

Back up the two JSON files before copying a newer package:

```bash
sudo systemctl stop memoria-bridge
sudo cp /opt/memoria-bridge/cameras.json \
  /home/administrator/cameras.json.backup
sudo cp /opt/memoria-bridge/inventory.json \
  /home/administrator/inventory.json.backup
sudo cp -a /home/administrator/memoria-warner-linux/. \
  /opt/memoria-bridge/
sudo chown -R memoria:memoria /opt/memoria-bridge
sudo chmod 600 \
  /opt/memoria-bridge/cameras.json \
  /opt/memoria-bridge/inventory.json
sudo systemctl start memoria-bridge
```

The release archive intentionally excludes deployed `cameras.json` and
`inventory.json`, so copying the package does not overwrite them.

## 16. Firewall guidance

Do not expose TCP port 8090 publicly. If UFW is enabled, allow only the
administrator LAN or VPN. Example:

```bash
sudo ufw allow from 192.168.80.0/24 to any port 8090 proto tcp
sudo ufw status
```

## Related documentation

- `STREAMING_ARCHITECTURE.md`: camera-to-LiveKit design and media flow.
- `TECHNICAL_SOP.md`: operation, capacity, security and troubleshooting.
- `README.md`: concise Linux quick start.
