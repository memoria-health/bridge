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
The page is TRANSPORT ONLY. It does not decode, render or encode anything.

1. Loads Warner transport and player scripts from `/sdk/`. No WASM decoder.
2. Connects using `Player.ConnectDevice(...)` over Warner cloud/P2P/TURN.
3. On a successful login callback, calls
   `ConnectApi.open_stream(session, channel, stream_id)`.
4. Warner delivers ENCODED frames through `ConnectApi.onrecvframeex` —
   video (frametype 1 keyframe, 2 delta) and audio (frametype 0) on the
   same callback.
5. The page prepends a one-byte type header and forwards each frame over
   a loopback WebSocket to Python.

`Player.init` is deliberately never called. It is what fills play.js's
`playerList`, and the decode/paint path at `play.js:288` is guarded on that
list being populated — so leaving it empty removes the WASM decoder and the
canvas, while `onrecvframeex`, which fires upstream in `433.connector.js`,
still delivers every frame. Two consequences follow:

- `Player.OpenStream` is replaced by `ConnectApi.open_stream`, because the
  Player wrapper ends in an UNGUARDED `playerList[winindex].open()`.
- `Player.OpenAudio` is dropped. It sets local speaker volume and `isSound`,
  and `isSound` appears nowhere in the connector — the camera sends audio
  over P2P regardless.

The frame tap is installed as a property with a setter rather than a plain
assignment, because the SDK assigns `onrecvframeex` itself after page load
and a plain assignment is silently replaced.

Some Warner bundles request assets from the site root, while others request
them under `/sdk/`. The Python application intentionally serves both forms:

```text
/sdk/j-decoder.wasm and /j-decoder.wasm
/sdk/mqtt/mqttWorker.js and /mqtt/mqttWorker.js
/sdk/logo_bg.png and /logo_bg.png
```

## Media conversion in Python

### Video

Encoded frames arrive on the WebSocket and go straight into ffmpeg, which
decodes to raw yuv420p on stdout. `CameraPublisher` reads fixed-size frames
and hands them to a LiveKit `VideoSource`, publishing as an ordinary `cam_*`
participant — no Ingress, so no per-minute ingest bill and no new participant
identity for the webhooks and `room_reconciler` to trip over.

The decoder's output size is PINNED with `-s WxH`. Frames are read in fixed
`width*height*3/2` chunks, so a camera streaming another size would desync
the reader permanently and every frame after the first would be garbage.

Audio is a SECOND ffmpeg process (ADTS AAC in, 48kHz mono PCM out) with its
own pump, so audio failing cannot take the picture a caregiver is watching.
The facilities audio kill cuts that lane at the source rather than only
unpublishing the track. The mobile
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

## One camera, one room

One physical camera is one database row, one `livekit_identity`, and ONE
room. Provisioning used to loop over every patient of the caregiver, so a
shared camera became N rows publishing N identical streams — N times the
ingest, N times the model spend on the same footage, duplicate events.

A camera covering several patients streams into the PRIMARY patient's room
only (`camera_patients`, one row flagged `is_primary`). The other covered
patients receive the RESULTS, never a second copy of the video. Multi-patient
is resolved on the subscriber side: the identity node scores a probe against
every enrolled patient on that camera.

```text
One Warner camera connection
        |
        +-> LiveKit identity -> primary patient's room
                                     |
                                     +-> results for every covered patient
```

This avoids duplicate vendor-cloud decoding per resident assignment, although
each LiveKit room still consumes outbound WebRTC bandwidth.

## What a camera costs

Measured 2026-08-21, not estimated. Two numbers that are not the same
thing, and reporting the first as the second overstates a fleet badly.

MARGINAL, per camera — this is what sets capacity:

| stage | memory | CPU |
|---|---|---|
| Chromium renderer (P2P transport only) | 144 MB | 0.03 cores |
| ffmpeg decode (2304x1296 H.265) | ~206 MB | 0.11 cores |
| LiveKit encode | (see below) | 0.28 cores |
| **total** | **~350 MB** | **~0.42 cores** |

SHARED, once per box: ~390 MB for the Chromium browser process, its GPU
and network helpers, and the Playwright driver. These are one set
however many cameras run; only the renderer scales.

How they were measured. `scripts/validate_strip.py` runs the real page
against a real camera and attributes RSS and CPU by process kind, which
is what separates the two tables above. `scripts/measure_encode.py` runs
the real publisher against a LOCAL LiveKit in dev mode with synthetic but
MOVING frames — a static picture lets the encoder skip nearly all its
work and understates the cost by an order of magnitude. The encode's
memory is deliberately not quoted per camera: that run measures one
process's peak RSS including the LiveKit FFI runtime, which is shared.

Of ffmpeg's share, only 56 MB is the decoder itself. The rest is
output-side frames, and most of it was ours: asyncio's StreamReader
defaults to a 64 KB limit, so reading 4.5 MB frames paused and resumed
the pipe about 67 times PER FRAME. Each pause blocks ffmpeg mid-write,
its queues back up, and AVBufferPool never releases — the high-water mark
of a momentary backlog becomes permanent RSS. Sizing the limit to one
frame takes it to zero pauses. Capping ffmpeg's threads, by contrast,
made it slightly worse; `video_argv` records every option tried and its
measured effect so nobody spends the day again.

Worth knowing before planning density: the real driver is how promptly
the reader drains the pipe, and that gets WORSE as cameras share one
event loop. Per-camera ffmpeg memory may drift back up on a loaded box.

At ~0.42 cores a six-core box holds roughly ten cameras with headroom,
and memory is not the binding constraint. Before the browser was stripped
to transport-only it was: the canvas route held a WASM decoder and a 3MP
canvas per camera.

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
- Runtime configuration, token and frame-ingest endpoints accept only
  loopback calls.
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

`publishing` proves track publication, not that camera pixels exist. The
frame counters are what verify the upstream stream. There is no canvas to
screenshot any more; the page renders nothing.

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
5. Check the per-camera rows on `/api/health` — state, frame age, restarts,
   and what was actually applied.
6. If frames are landing, inspect the mobile subscriber's exact
   room/identity and track attachment.

Commands:

```bash
sudo journalctl -u memoria-warner-bridge --since "5 minutes ago" --no-pager \
  | grep -E 'warner video frames|worker|Open stream|publishing|HTTP 404|Aborted'

```

Interpretation:

- No worker: missing roster mapping/service failure (not mobile state in
  continuous mode).
- Worker and publisher but no frames: wrong channel/stream or camera transport.
- `publishing` with a stale frame counter: silent Warner decoder freeze; confirm
  a subsequent `VIDEO STALLED` event and automatic worker recovery.
- Frames landing but blank app: wrong LiveKit room/identity, subscriber UI, or
  a private camera whose allow-list does not include the subscriber.
- Repeated disconnect/reconnect reason 2: duplicate publisher identity.
- HTTP 404 for WASM/worker assets: wrong/old bridge server build.

## Confirmed production failure and resolution

The tested camera initially used `channel=1, stream_id=1`. Warner reported that
the stream-open command succeeded but no frame callback occurred. Changing to
`channel=0` immediately produced continuous frame counters, which confirms that
Warner channel numbers are zero-based physical channels and that
single-channel cameras must use channel zero.

CORRECTION (2026-08-21). The same change also moved stream_id from 1 to 0, and
that half was wrong. **stream_id 0 is the SUB stream**, which caps at
`800x448` — the `800x448 H265` counters this section originally cited as proof
of success were proof of the downgrade. **stream_id 1 is MAIN**, measured at
`2304x1296 H265`. Use `channel=0, stream_id=1`.

The mistake survived because `800x448` frames flowing looks identical to
success if you are only counting frames. It cost every camera provisioned
before 2026-08-20 its resolution, on 3MP sensors, upstream of a detector whose
face matching depends on pixel height.

## Confirmed silent-decoder failure and recovery

Production observation showed Warner H.265/WASM frame callbacks stopping while
the service, roster, P2P connection and LiveKit publishers remained active. No
vendor disconnect callback fired. The frame watchdog detects missing frame
heartbeats, recreates the affected browser worker and restores publication
without restarting the complete bridge.
