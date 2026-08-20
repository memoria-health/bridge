#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/memoria-warner-bridge}"
APP_USER="${APP_USER:-memoria}"
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo bash scripts/install.sh" >&2; exit 1; fi
if ! id "$APP_USER" >/dev/null 2>&1; then useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"; fi
apt-get update
apt-get install -y python3 python3-venv python3-pip curl ca-certificates
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/browsers" "$APP_DIR/.venv/bin/python" -m playwright install-deps chromium
sudo -H -u "$APP_USER" env PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/browsers" "$APP_DIR/.venv/bin/python" -m playwright install chromium
if [ ! -f "$APP_DIR/cameras.json" ]; then
  cp "$APP_DIR/gateway-config.example.json" "$APP_DIR/cameras.json"
  echo "WARNING: edit $APP_DIR/cameras.json before starting the service."
fi
mkdir -p "$APP_DIR/logs"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
cp "$APP_DIR/deploy/memoria-warner-bridge.service" /etc/systemd/system/
systemctl daemon-reload
echo "Installation complete. Edit cameras.json, then run:"
echo "sudo systemctl enable --now memoria-warner-bridge"
