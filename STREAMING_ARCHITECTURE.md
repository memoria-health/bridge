# Warner/Trueview Camera to LiveKit Streaming Architecture

## Document purpose

This document describes how the Memoria Warner Bridge moves live video and
audio from geographically distributed Warner/Trueview cameras into LiveKit.
It covers the production data path, control path, security boundaries,
continuous streaming model, multi-camera behavior and troubleshooting.

## System context

Each camera connects to local Wi-Fi at its geographical location and registers
with Warner/Trueview Cloud. The central bridge does not require LAN access to
the camera. It requires Internet access to Warner Cloud, the Nemara backend and
LiveKit Cloud.

```text
Camera site                                       Central infrastructure

Camera -> Wi-Fi -> Internet -> Warner Cloud/P2P -> Memoria Warner Bridge
                                                        |
                                                        +-> Nemara backend roster
                                                        |
                                                        +-> LiveKit Cloud room
                                                               |          |
                                                               v          v
                                                          Mobile app   AI backend
```

## Why a browser runtime is required

The licensed Warner H5 SDK is JavaScript/WebAssembly software designed for a
browser. It implements vendor authentication, cloud/P2P/TURN transport, stream
framing, H.264/H.265 decoding and PCM audio handling. Python cannot replace
that SDK without a separate native vendor API.

The bridge therefore uses Playwright to supervise headless Chromium. Every
physical camera gets an independent browser context. No visible browser window
or administrator page needs to remain open.

Separate contexts are mandatory because Warner pauses decoding when a tab is
hidden. Putting twenty cameras in twenty tabs of one context can leave only one
camera rendering. Twenty separate contexts keep all twenty active.

## Control plane

### Provisioning

An administrator opens `http://BRIDGE-IP:8090/`, signs in using the Warner SDK,
selects a device and submits:

- caregiver/resident email;
- camera name;
- Warner device key (`productId:deviceId:deviceSecret`);
- camera username/password;
- channel and stream ID.

For a normal single-channel camera, use:

```text
channel = 0
stream_id = 0  (substream, confirmed 800x448 H265)
```

`stream_id=1` is the main stream when supported. `channel=1` is the second
physical channel, not “camera number one,” and produces a blank stream on a
single-channel camera.

Python calls the Nemara endpoint `/v1/gateway/provision-camera` using the
gateway bearer token. The backend creates/returns room-scoped camera identities.
The bridge persists the physical camera and returned identities in
`inventory.json`.

### Backend roster

The bridge polls `/v1/gateway/roster`. The roster authorizes which LiveKit
identities and rooms belong to this gateway. Local configuration and roster
must both contain an identity before the bridge publishes it.

The production configuration uses:

```json
"stream_mode": "continuous"
```

In continuous mode, every locally provisioned identity found in the authorized
roster is published regardless of the roster's transient mobile
`desired_state`. This is required because AI analysis continues while the
mobile app is closed. AI Pause controls the AI subscriber/analysis process; it
does not remove the camera publisher.

An optional `policy` mode exists for future backends with reliable combined
mobile/AI demand fields, but it is not the production default.

## Media plane: camera to browser

For every active physical camera, Playwright loads `/runtime?camera_id=...`.
The runtime performs the following sequence:

1. Loads Warner transport, decoder and player scripts from `/sdk/`.
2. Connects using `Player.ConnectDevice(...)` over Warner cloud/P2P/TURN.
3. Receives a successful Warner login callback.
4. Calls `Player.OpenStream(device_id, "", channel, stream_id, 0)`.
5. Warner delivers compressed video frames through
   `ConnectApi.onrecvframeex`.
6. Warner's WASM decoder converts H.264/H.265 frames and renders them to an
   HTML canvas.
7. Warner delivers decoded PCM audio to its audio-player feed.

The bridge forces Warner's WASM decoder in headless Chromium. Its WebCodecs
path transfers rendering to an OffscreenCanvas and can leave
`canvas.captureStream()` publishing a black picture.

Some Warner bundles request assets from the site root, while others request
them under `/sdk/`. The Python application intentionally serves both forms:

```text
/sdk/j-decoder.wasm and /j-decoder.wasm
/sdk/mqtt/mqttWorker.js and /mqtt/mqttWorker.js
/sdk/logo_bg.png and /logo_bg.png
```

## Media conversion inside Chromium

### Video

The source canvas is captured at 25 frames per second:

```javascript
videoStream = canvas.captureStream(25)
```

The first video media track is cloned and wrapped as a LiveKit
`LocalVideoTrack`. LiveKit receives browser-compatible raw frames after the
Warner decoder; LiveKit/WebRTC performs its own outbound encoding. The mobile
app does not need to decode the camera's original H.265 bitstream directly.

### Audio

Warner supplies PCM samples through `JAaudioplay.pcmPlayer.feed`. The bridge
wraps that feed while preserving Warner's original playback function. Samples
are scheduled into an `AudioContext`, connected to a
`MediaStreamDestination`, and its audio track becomes a LiveKit
`LocalAudioTrack`.

The published source types are:

```text
Video track source: Camera
Audio track source: Microphone
```

## LiveKit authentication and publishing

LiveKit API credentials stay in the Python process and protected
`cameras.json`. They are never placed in static JavaScript.

Before joining, the browser requests a token from the local endpoint:

```text
POST /api/runtime/{camera_id}/token
```

Python verifies that the requested `(identity, room)` pair is both locally
mapped and backend-authorized. It then generates a short-lived room-scoped JWT
with `room_join=true`.

The browser connects to the configured `wss://...livekit.cloud` signaling URL
and publishes cloned video and audio tracks. A per-identity connection lock
prevents overlapping joins. Without this lock, duplicate participants with the
same identity eject one another and repeatedly unpublish tracks.

## Fan-out

One physical Warner camera can be assigned to several LiveKit identities and
rooms. The bridge opens the vendor stream only once, then clones the same
canvas/audio capture into several LiveKit `Room` connections:

```text
One Warner camera connection
        |
        +-> LiveKit identity A -> patient room A
        +-> LiveKit identity B -> patient room B
        +-> LiveKit identity C -> patient room C
```

This avoids duplicate vendor-cloud decoding per resident assignment, although
each LiveKit room still consumes outbound WebRTC bandwidth.

## Continuous operation

The Python service and its headless Chromium workers must remain running. The
administrator webpage can be closed after provisioning.

Linux uses `memoria-warner-bridge.service`; Windows uses the
`MemoriaWarnerBridge` startup task. Both restart the bridge after crashes or
reboots.

With twenty provisioned cameras in continuous mode, expect twenty Warner cloud
connections and decoders. LiveKit publisher count may be higher if cameras fan
out to multiple rooms. Capacity-test 1, 5, 10 and 20 cameras while monitoring
CPU, RAM and both inbound and outbound bandwidth.

## Security boundaries

- `cameras.json` contains LiveKit server credentials and the gateway token.
- `inventory.json` contains Warner device secrets and camera passwords.
- Both files must be readable only by the service account.
- Port 8090 must be limited to an administrator LAN/VPN and never exposed
  directly to the public Internet.
- Runtime configuration/token/screenshot endpoints accept only loopback calls.
- Generated LiveKit tokens are constrained to an authorized identity and room.
- Rotate credentials that appear in chat, documentation, screenshots or logs.

## Health and observability

Query:

```bash
curl -s http://127.0.0.1:8090/api/health | python3 -m json.tool
```

Healthy continuous output includes:

```json
{
  "ok": true,
  "stream_mode": "continuous",
  "stream_reasons": ["continuous_bridge"],
  "state": "publishing",
  "frame_age_seconds": 2.2,
  "frame_count": 8770,
  "restart_count": 0
}
```

Important runtime log milestones:

```text
browser worker started
[Decode Method] wasm ffmpeg working...
login succeeded
Open stream succeeded at channel 0 Streamid 0
warner video frames: 1 800x448 H265
connected to LiveKit Server
publishing
```

`publishing` proves track publication, not that camera pixels exist. Frame
counters and the screenshot endpoint verify the upstream canvas.

### Video-frame watchdog

The runtime sends a `frame_heartbeat` to the loopback status API at least every
five seconds while Warner's encoded-frame callback is advancing. The Python
supervisor checks every two seconds. A worker is considered frozen when it
previously produced frames but `frame_age_seconds` exceeds 20 seconds, or when
it does not produce its first frame within the 45-second startup grace period.

The supervisor closes only that camera's Playwright page/context and creates a
new isolated worker. The Python service, backend roster, HTTP API and other
camera workers remain running. Recovery is logged as `VIDEO STALLED`, and
`restart_count`/`last_restart_at` are exposed through `/api/health`.

Defaults can be overridden with `WARNER_FRAME_STALL_SECONDS` and
`WARNER_FRAME_STARTUP_GRACE_SECONDS`. Avoid a stall threshold below 15 seconds,
because ordinary network jitter could cause unnecessary reconnects. Browser
logs redact LiveKit `access_token` and `join_request` query values.

## Blank-video diagnostic sequence

1. Confirm `/api/health` reports `stream_mode=continuous` and a worker.
2. Confirm `state=publishing`.
3. Search logs for `warner video frames`.
4. Verify `/j-decoder.wasm`, `/mqtt/mqttWorker.js` and `/logo_bg.png` return 200.
5. Capture the local diagnostic screenshot.
6. If frames and screenshot are good, inspect the mobile subscriber's exact
   room/identity and track attachment.

Commands:

```bash
sudo journalctl -u memoria-warner-bridge --since "5 minutes ago" --no-pager \
  | grep -E 'warner video frames|worker|Open stream|publishing|HTTP 404|Aborted'

curl --fail \
  http://127.0.0.1:8090/api/debug/CAMERA_ID/screenshot \
  --output /tmp/warner-debug.png
```

Interpretation:

- No worker: missing roster mapping/service failure (not mobile state in
  continuous mode).
- Worker and publisher but no frames: wrong channel/stream or camera transport.
- `publishing` with a stale frame counter: silent Warner decoder freeze; confirm
  a subsequent `VIDEO STALLED` event and automatic worker recovery.
- Frames but black screenshot: decoder/rendering problem.
- Good screenshot but blank app: wrong LiveKit room/identity or subscriber UI.
- Repeated disconnect/reconnect reason 2: duplicate publisher identity.
- HTTP 404 for WASM/worker assets: wrong/old bridge server build.

## Confirmed production failure and resolution

The tested camera initially used `channel=1, stream_id=1`. Warner reported that
the stream-open command succeeded, and LiveKit published tracks, but the canvas
remained blank and no frame callback occurred. Changing to
`channel=0, stream_id=0` immediately produced continuous `800x448 H265` frame
counters. This confirms that Warner channel numbers are zero-based physical
channels and that single-channel cameras must normally use channel zero.

## Confirmed silent-decoder failure and recovery

Production observation showed Warner H.265/WASM frame callbacks stopping while
the service, roster, P2P connection and LiveKit publishers remained active. No
vendor disconnect callback fired. The frame watchdog detects missing frame
heartbeats, recreates the affected browser worker and restores publication
without restarting the complete bridge.
