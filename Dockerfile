# The bridge as a cloud service.
#
# It runs equally well on a rented container as on the Ubuntu box in
# LINUX_INSTALLATION.md, because it never touches the camera's LAN:
# STREAMING_ARCHITECTURE.md — "The central bridge does not require LAN
# access to the camera. It requires Internet access to Warner Cloud, the
# Nemara backend and LiveKit Cloud." All three are outbound.
#
# What it DOES need is a real Chromium (the vendor SDK is a browser
# runtime), ffmpeg (one decoder process per camera), and somewhere
# durable to keep `inventory.json` — the provisioned camera list, which
# holds each unit's device key and admin password. On a platform with an
# ephemeral filesystem that last one is a VOLUME, or every redeploy
# un-provisions the fleet.
#
# systemd stays the reference deployment. This is the same program with
# the same env; nothing here is a fork.

FROM python:3.12-slim

# ffmpeg is THE DECODER. Since the browser was stripped to transport
# only, every camera is one ffmpeg process and a box without it runs no
# cameras at all — while still answering /api/health and polling the
# roster perfectly well.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# OUTSIDE the app directory, so a bind-mount or a volume over /app can
# never hide the browser. This is the failure the README opens with:
# without it Playwright looks in ~/.cache/ms-playwright, finds nothing,
# and `vendor.start()` raises `Executable doesn't exist` — invisible
# from outside, because the bridge still answers, still polls, still
# lists its cameras, and publishes nothing.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers

WORKDIR /app

# Requirements first so a code change does not re-download Chromium.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium

COPY . .

# `browsers/` is gitignored and `install.sh` populates it on the Ubuntu
# path. In the image it lives at PLAYWRIGHT_BROWSERS_PATH instead, so an
# empty or stale copy of that directory must not travel in the context
# and shadow it. See .dockerignore.

# Defaults for a container. Every one is overridable from the platform's
# environment; the two that MUST be set there are BACKEND_URL and
# LIVEKIT_URL (validate_settings refuses to start without them).
ENV BRIDGE_VENDOR=warner \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=8090 \
    BRIDGE_INVENTORY=/data/inventory.json \
    BRIDGE_IDENTITY=/data/identity.json \
    PYTHONUNBUFFERED=1

# /data is where the durable state lives. Both files in it hold live
# credentials: the identity is this instance's bearer token, the inventory
# each camera's device key and admin password.
#
# There is deliberately NO `VOLUME ["/data"]` here. Railway rejects the
# directive at parse time -- "docker VOLUME at Line 64 is not supported,
# use Railway Volumes" -- and does so BEFORE any build step runs, so the
# deploy fails having emitted nothing but "scheduling build". Four builds
# died that way on 2026-08-25 with no log to explain it.
#
# Nothing is lost by dropping it. VOLUME only ever declared an ANONYMOUS
# volume for a bare `docker run` with no -v; it never created the mount
# that matters. The mount is supplied by the platform (a Railway Volume at
# /data) or by `-v` locally, and the entrypoint already mkdir -p's the
# directory and REFUSES TO START if it is not writable -- which is the
# real guard, and a louder one than an implicit volume.

EXPOSE 8090

# Not `python bridge.py` directly: the platform names the port and the
# public origin at RUN time, and the vendor page loads itself from that
# origin. The entrypoint reconciles those before exec'ing the bridge.
ENTRYPOINT ["/app/deploy/container-entrypoint.sh"]
