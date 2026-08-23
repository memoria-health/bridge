# Running the bridge as a hosted service

`LINUX_INSTALLATION.md` is still the reference deployment and this does
not replace it. It is the same program, same environment variables, same
`/api/health`. What changes is that a platform names the port, wipes the
filesystem on every deploy, and puts a router in front of the process.

Written against Railway, because that is what we use. Nothing here is
Railway-specific except `railway.json`; Fly, Cloud Run and a plain
`docker run` all work from the same image.

## Why this is possible at all

The bridge never touches the camera's LAN. From
`STREAMING_ARCHITECTURE.md`:

> The central bridge does not require LAN access to the camera. It
> requires Internet access to Warner Cloud, the Nemara backend and
> LiveKit Cloud.

All three are outbound, so the bridge can live anywhere with internet.
It is not an edge appliance the way `nemara-gateway` is — that one pulls
RTSP off cameras on the same network and cannot leave the house.

## Deploy

1. Point a Railway service at this repo. `railway.json` selects the
   Dockerfile builder and health-checks `/api/health`.
2. Attach a **volume** mounted at `/data`.
3. Set the environment below.
4. Open `https://<service>/?key=<BRIDGE_KEY>` and provision cameras
   exactly as on the Ubuntu box.

### Environment

| | |
|---|---|
| `BACKEND_URL` | **required** — the bridge refuses to start without it |
| `LIVEKIT_URL` | **required** — `wss://…livekit.cloud` |
| `BRIDGE_KEY` | **required here.** Bound wide with no key the admin page is closed to everyone, so you cannot provision |
| `BRIDGE_HOST` | `0.0.0.0` (already the image default) |
| `BRIDGE_INVENTORY` | `/data/inventory.json` (image default) |
| `BRIDGE_IDENTITY` | `/data/identity.json` (image default) |
| `PORT` | injected by the platform; the entrypoint maps it to `BRIDGE_PORT` |

`BRIDGE_ORIGIN` is set by the entrypoint and you should not override it.
It is where our own Chromium loads `/runtime` from, **inside** the
container, so it must be loopback. Setting it to the public https URL
would route every camera's page load out to the edge and back, pay
egress for it, and make frame decoding depend on the router being
healthy.

`BRIDGE_TOKEN` is available to inject the instance's bearer token from
the environment instead of minting one on disk. Only reach for it if you
are re-creating a bridge whose backend row already exists — a fresh
deployment mints its own on first boot and persists it to the volume,
which is what you want.

## Four things that will bite

**The volume is not optional.** `inventory.json` is the provisioned
camera list and holds each unit's device key and admin password. Without
a volume it lives on the container filesystem and every redeploy
un-provisions the fleet — which surfaces much later as "the bridge
forgot every camera" with nothing pointing at the deploy that did it.

**One replica.** `railway.json` pins `numReplicas: 1`. Two replicas
share `BRIDGE_TOKEN` and publish the same cameras into the same LiveKit
room under the same participant identity, and LiveKit permits one
participant per identity per room — so each new connection disconnects
the other, forever, and neither ever delivers a stable stream. Scaling
this service horizontally is not a capacity lever; it is an outage.

**`/api/health` is unauthenticated.** It is the only route without a
check, because the platform's health probe cannot carry a key. On a
public domain that means the camera list — uuids, room names, connection
state — is readable by anyone with the URL. Accept it, or put the
service behind private networking and provision through a Railway proxy.

**A green health check does not mean it works.** This is the failure
this repo keeps producing: with no Chromium the bridge still answers
`/api/health`, still polls the roster, still lists its cameras, and
publishes nothing. `railway.json` probes `/api/health`, so it will call
that deployment healthy. The real check is `scripts/health_check.py`,
which fails on `configured_cameras > 0` with an empty `entities` array.
Run it against the deployment, not just the probe:

```bash
curl -s https://<service>/api/health | python scripts/health_check.py
```

## What has NOT been proven

The image builds and boots: `headless Chromium started`, the port
mapping takes, `/data` is writable, ffmpeg and Chromium are both present
at `/opt/browsers`, and `health_check.py` correctly exits non-zero on an
unreachable roster.

**Not yet proven: a real camera streaming from a cloud host.** The
vendor transport is WebRTC over Warner cloud/P2P/TURN, and a container
sits behind NAT with no inbound UDP. TURN relay should carry it — that
fallback is what the vendor SDK is for — but "should" is not a soak.
Provision one camera and watch `last_frame_age_s` on `/api/health`
before moving anything real onto this path.

Cost note while you are watching it: each camera is one Chromium page
plus one ffmpeg process, and each LiveKit room consumes outbound WebRTC
bandwidth per subscriber. Size the plan against camera count, not
request volume.
