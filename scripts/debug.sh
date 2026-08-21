#!/usr/bin/env bash
set -u
echo "=== Service ==="
systemctl status memoria-warner-bridge --no-pager || true
echo "=== Port 8090 ==="
ss -ltnp | grep ':8090' || true
echo "=== Processes ==="
pgrep -af 'bridge.py|chromium|playwright' || true
echo "=== Health ==="
curl --silent --show-error http://127.0.0.1:8090/api/health || true
echo
echo "=== Last 100 service log lines ==="
journalctl -u memoria-warner-bridge -n 100 --no-pager || true
