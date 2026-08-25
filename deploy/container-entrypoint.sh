#!/bin/sh
# Reconcile what the PLATFORM decides at run time, then exec the bridge.
#
# Three things a container host names for us and a systemd unit does not:
# the port, the writable path, and the address our own Chromium should
# load the vendor page from. Getting the third wrong is the interesting
# one, so it is spelled out below.
set -eu

# Railway, Fly, Cloud Run and Heroku all inject $PORT and expect the
# process to listen on exactly that. BRIDGE_PORT wins if it was set
# deliberately, so a fixed-port deployment is still possible.
if [ -n "${PORT:-}" ] && [ "${BRIDGE_PORT:-8090}" = "8090" ]; then
  BRIDGE_PORT="$PORT"
  export BRIDGE_PORT
fi

# BRIDGE_ORIGIN is where OUR OWN Chromium loads /runtime from --
# `vendors/warner/chromium.py` does `page.goto(f"{PUBLIC_ORIGIN}/runtime
# ?camera_id=...")` from inside this container. So it must be the
# LOOPBACK address, never the platform's public https URL: sending it
# out to the edge and back in would depend on the router being healthy
# to decode a frame, would pay egress on every camera's page load, and
# on a private service would simply not resolve.
: "${BRIDGE_ORIGIN:=http://127.0.0.1:${BRIDGE_PORT:-8090}}"
export BRIDGE_ORIGIN

# The volume. Missing here means the provisioned camera list is written
# to the container filesystem and lost on the next deploy, which reads
# as "the bridge forgot every camera" long after the deploy that did it.
DATA_DIR="$(dirname "${BRIDGE_INVENTORY:-/data/inventory.json}")"
mkdir -p "$DATA_DIR"
if [ ! -w "$DATA_DIR" ]; then
  echo "bridge: $DATA_DIR is not writable — provisioning cannot persist" >&2
  exit 1
fi

# Say the posture out loud at boot. Bound wide with no key, the bridge
# closes the admin page to everyone (fail closed, core/config.py) and
# cameras already provisioned keep publishing -- which looks fine until
# somebody tries to add one.
case "${BRIDGE_HOST:-127.0.0.1}" in
  127.0.0.1|localhost) ;;
  *)
    if [ -z "${BRIDGE_KEY:-}" ]; then
      echo "bridge: BRIDGE_HOST=${BRIDGE_HOST} with no BRIDGE_KEY — the" >&2
      echo "        admin page and provisioning are CLOSED. Set BRIDGE_KEY." >&2
    fi
    ;;
esac

exec python /app/bridge.py
