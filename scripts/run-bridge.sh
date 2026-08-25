#!/usr/bin/env bash
# Start the bridge with the environment it actually needs.
#
# Written 2026-08-25 after a restart to pick up a fix took the TV Room
# offline for several minutes. The process had been started by hand
# months earlier and three required values lived only in that one shell:
# BACKEND_URL, LIVEKIT_URL and PLAYWRIGHT_BROWSERS_PATH. Killing it threw
# them away. Nothing on disk recorded them, so the camera stayed down
# while they were reconstructed one failure at a time.
#
# Nothing here is a secret. LIVEKIT_URL is read from the backend's .env
# and BACKEND_URL is discovered from the running ngrok tunnel, so neither
# is written down anywhere it could be committed.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${BACKEND_DIR:-$(cd "$APP_DIR/../nemara-backend" && pwd)}"

# The bridge reaches the backend through the ngrok tunnel, not over the
# LAN, because the Mac's LAN address changes with the network and the
# tunnel does not. Ask ngrok what it is currently serving rather than
# pinning a URL that expires every restart.
if [[ -z "${BACKEND_URL:-}" ]]; then
  BACKEND_URL="$(curl -s -m 5 http://127.0.0.1:4040/api/tunnels \
    | "$APP_DIR/.venv/bin/python" -c \
      'import json,sys; ts=json.load(sys.stdin)["tunnels"]; print(next(t["public_url"] for t in ts if t["public_url"].startswith("https")))' \
    2>/dev/null || true)"
fi
if [[ -z "${BACKEND_URL:-}" ]]; then
  echo "BACKEND_URL unset and no https ngrok tunnel found on :4040." >&2
  echo "Start ngrok, or export BACKEND_URL yourself." >&2
  exit 1
fi

# LiveKit is the same project the backend uses; one source of truth.
if [[ -z "${LIVEKIT_URL:-}" && -f "$BACKEND_DIR/.env" ]]; then
  LIVEKIT_URL="$(grep -E '^NEMARA_LIVEKIT_URL=' "$BACKEND_DIR/.env" | cut -d= -f2-)"
fi
if [[ -z "${LIVEKIT_URL:-}" ]]; then
  echo "LIVEKIT_URL unset and not found in $BACKEND_DIR/.env" >&2
  exit 1
fi

# what the camera actually sends, and size the fixed-length frame reads
# from ffmpeg (vendors/warner/source.py:check_stream). Setting them to
# anything other than the camera's real output shifts every frame after
# the first by the remainder, forever — the source logs STREAM MISMATCH
# and says plainly that it does not recover. Pinned to 1920x1080 on
# 2026-08-25 to "match the Tapo"; the camera sends 2304x1296, so it was
# corrupting frames rather than resizing them. Left unset deliberately.

export PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/browsers"
export BACKEND_URL LIVEKIT_URL

# Wait for a previous process to release the port before starting.
# Without this, stop-then-start races: the dying bridge still holds
# 8090, the new one fails to bind, the old one exits a second later, and
# the camera is left OFF with both processes gone. That took the TV Room
# off the soak on 2026-08-25.
# A LISTENER, not "any socket on this port". `lsof -ti :PORT` also
# matches CLIENT sockets — including CLOSED ones a dead connection left
# behind. VS Code's port forwarding keeps a fistful of those against
# whatever it has ever attached to, so on 2026-08-26 this guard refused
# to start the bridge while nothing whatsoever was listening, and the TV
# Room stayed down because of the check meant to protect it.
listener() {
  lsof -nP -iTCP:"${BRIDGE_PORT:-8090}" -sTCP:LISTEN -t 2>/dev/null
}
for _ in $(seq 1 20); do
  [ -z "$(listener)" ] && break
  sleep 1
done
if [ -n "$(listener)" ]; then
  echo "port ${BRIDGE_PORT:-8090} still LISTENing after 20s; refusing to start a second bridge" >&2
  exit 1
fi

mkdir -p "$APP_DIR/logs"
LOG="$APP_DIR/logs/bridge-$(date +%Y%m%d-%H%M%S).log"

# nohup in a subshell, so the bridge outlives the shell that started it.
# Not `setsid` — that is Linux-only and this runs on a Mac, where its
# absence is a hard error rather than a fallback.
#
# Note for anyone stopping it: `ps` reports the interpreter by its real
# path, which for a venv is the base Python with a CAPITAL P. So
# `pkill -f "python bridge.py"` matches nothing and silently leaves the
# old process running while you start a second one. Match on `bridge\.py`.
cd "$APP_DIR"
( nohup "$APP_DIR/.venv/bin/python" bridge.py >"$LOG" 2>&1 < /dev/null & )

echo "bridge starting, log: $LOG"
