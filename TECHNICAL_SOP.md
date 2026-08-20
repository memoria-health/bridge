# Memoria Warner Cloud to LiveKit Bridge

## 1. Purpose and scope

This package connects geographically distributed Warner/Trueview cameras to
the rooms assigned by the Nemara backend. Cameras remain connected to their
vendor cloud over local Wi-Fi. A single Internet-connected Windows or Linux
bridge retrieves required streams through the licensed Warner H5 SDK, decodes
video/audio, and republishes them to LiveKit for the mobile application and the
continuous AI-analysis backend.

The administrator page is used only to provision a camera. It does not need to
remain open. The bridge process must remain running as a background service.

## 2. Architecture

```text
Remote camera -> Wi-Fi/Internet -> Warner/Trueview cloud
                                      |
                                      v
Central bridge -> hidden Chromium + Warner H5 SDK -> canvas/PCM
                                      |
                                      v
                         LiveKit WebRTC publisher
                                      |
                                      v
                    assigned LiveKit room -> mobile app and AI backend
```

Python performs backend roster polling, provisioning, camera persistence,
short-lived LiveKit publisher-token generation, health reporting, and Chromium
supervision. Warner transport and decoding remain in JavaScript/WebAssembly
because the vendor SDK is browser-based.

Each active physical camera receives a separate browser context. This is
required because the Warner SDK pauses video decoding in hidden tabs. WebCodecs
is disabled inside workers so the SDK uses its WASM canvas path; otherwise a
headless OffscreenCanvas can produce a valid but black captured track.

## 3. Continuous publishing lifecycle

Provisioning stores a physical camera and its backend-assigned LiveKit
identities. In the production default, `stream_mode` is `continuous`: each
provisioned physical camera remains connected to Warner Cloud and publishes to
all of its assigned LiveKit rooms. Closing the mobile app or changing its live
view state does not remove the publisher.

AI pause controls whether the AI backend subscribes to/analyses the available
LiveKit stream. It does not tear down the central camera publisher. This avoids
blank video and reconnection delays when backend `desired_state` temporarily
changes to `disconnected` as the mobile application closes.

Example roster entries:

```json
{
  "livekit_identity": "cam_ab12cd34",
  "livekit_room": "resident-room",
  "desired_state": "disconnected"
}
```

Even with the roster state above, continuous mode publishes that provisioned
identity and reports `stream_reasons: ["continuous_bridge"]`.

An optional `stream_mode: "policy"` is retained for installations whose backend
reliably combines mobile and AI demand. Policy mode understands
`desired_state`, `viewer_count`, `mobile_active`, `ai_enabled`, `ai_paused`,
`analysis_enabled`, and `analysis_paused`. Do not use policy mode for this
deployment until the backend exposes durable AI state.

If twenty cameras are provisioned, all twenty Warner streams and decoders stay
active continuously. A physical camera assigned to multiple rooms uses one
Warner connection and fans the same capture out to multiple LiveKit publishers.

## 4. Package contents

- `warner_bridge.py`: provisioning API, roster, token and worker supervisor.
- `warner_bridge_static/`: administrator and hidden runtime pages.
- `warner_sdk/`: licensed vendor JavaScript/WASM SDK.
- `cameras.json`: gateway/backend/LiveKit configuration; secret, created locally.
- `warner_cameras.json`: provisioned device keys/passwords; secret, generated.
- `gateway-config.example.json`: configuration template without credentials.
- `scripts/`: installation, startup, health and diagnostic tools.

Back up `cameras.json` and `warner_cameras.json` securely. Never commit them or
send them in support screenshots.

## 5. Prerequisites

- Stable wired Internet for the central bridge where possible.
- Remote cameras online in Warner/Trueview cloud via Wi-Fi.
- Valid camera device key in `productId:deviceId:deviceSecret` format.
- Camera username/password and correct channel/stream ID.
- Nemara backend URL and gateway token.
- LiveKit URL, API key and API secret.
- Python 3.10 or newer.
- For twenty simultaneously active streams: begin with 8 modern CPU cores,
  16 GB RAM, and adequate upstream/downstream bandwidth; measure actual usage.

## 6. Configuration

Copy `gateway-config.example.json` to `cameras.json` and fill all fields:

```json
{
  "backend_url": "https://backend.example.com",
  "gateway_token": "secret gateway bearer token",
  "livekit_url": "wss://project.livekit.cloud",
  "livekit_api_key": "server API key",
  "livekit_api_secret": "server API secret",
  "poll_seconds": 3
}
```

The LiveKit API secret stays on the bridge. It is never inserted into browser
JavaScript. Python creates room-scoped publisher JWTs only after validating the
identity/room against the authorized backend roster.

## 7. Provisioning SOP

1. Confirm the bridge health endpoint is responding.
2. Open `http://BRIDGE-IP:8090/` from the administrator LAN/VPN.
3. Enter the resident/user email expected by the Nemara backend.
4. Enter a meaningful camera/room name.
5. Enter the Warner device key, camera account, password, channel and stream.
6. Submit once. Record the returned `camera_id` and LiveKit identities.
7. Enable AI analysis or request live view from the mobile application
   (https://apps.apple.com/app/id6773233275).
8. Confirm health progresses through `camera_connecting`, `camera_connected`,
   `livekit_connecting`, and `publishing`.
9. Confirm video and audio in the mobile application.

Provisioning is idempotent by physical device identity. Repeating the same key
returns the existing camera instead of creating a duplicate.

## 8. Expected health states

- In continuous mode, every provisioned camera with a backend roster mapping
  should have a worker. An absent worker is an error or missing roster mapping.
- `camera_connecting`: vendor-cloud/P2P connection is starting.
- `camera_connected`: vendor login succeeded and stream was opened.
- `livekit_connecting`: room-scoped token obtained; WebRTC is connecting.
- `publishing`: video and audio tracks were published.
- `video_stalled`: no encoded video frame arrived within the configured
  threshold; the supervisor is recreating the affected camera worker.
- `camera_login_failed`: device key/account/password/channel is wrong.
- `camera_p2p_error`: vendor-cloud reachability or device availability problem.
- `reconcile_error`: backend, token or LiveKit operation failed.
- `livekit_disconnected`: network interruption or participant replacement.

## 9. Debugging decision tree

### Room absent from the mobile application

Check the provisioning response, backend roster and gateway token. Confirm the
email maps to the expected resident and the backend returned identities.

### Room exists but no LiveKit participant

Confirm `/api/health` says `stream_mode: continuous` and reports
`continuous_bridge` for the identity. If no worker exists, inspect roster
errors, identity mappings and backend authorization.

### Mobile app is closed and AI analysis stops

Confirm `stream_mode` is `continuous`. The publisher should remain present;
AI pause/resume is handled by the AI subscriber and must not remove the camera
publisher.

### Participant exists but video is black

Confirm logs contain `wasm ffmpeg working`, not `webcodecs working`. Confirm
each camera has its own worker and that camera frames are arriving. Check device
channel/stream ID. A LiveKit track can exist even when the vendor canvas is not
receiving decoded frames.

### Only one of several active cameras has video

This indicates hidden-tab decoding or an old bridge build. The supplied build
uses one browser context per active camera. Restart the service after replacing
code; copying files does not reload already-open workers.

### Repeated connect/disconnect with LiveKit reason 2

Two publishers are using the same identity. Ensure only one bridge instance is
running. The runtime includes an identity connection lock, but a second service
or manually launched copy can still duplicate the identity.

### Audio is missing

Confirm `Player.OpenAudio(0)` succeeds, Warner PCM frames arrive, and the room
shows a microphone-source track. Browser autoplay is enabled by the bridge.

### Port 8090 is unavailable

Find the existing process. Do not run a manual bridge and the background
service simultaneously.

### High CPU, delays or freezing

Count simultaneously connected cameras, inspect Chromium processes, CPU/RAM,
packet loss and bandwidth. Reduce resolution/stream ID or divide continuously
published cameras across multiple bridges. Pausing AI alone does not stop the
publisher in continuous mode.

### Publisher is connected but video stops

1. Query `/api/health` and inspect `frame_count` and `frame_age_seconds`.
2. Repeat after five seconds. A healthy `frame_count` increases and frame age
   normally remains below five seconds.
3. If frame age passes 20 seconds, search logs for `VIDEO STALLED`.
4. Confirm the worker reconnects, `frame_count` starts again and
   `restart_count` increments.
5. If no automatic recovery occurs after the 45-second startup grace period,
   restart the complete service/task and collect decoder/P2P logs for Warner.

Linux commands:

```bash
curl -s http://127.0.0.1:8090/api/health | python3 -m json.tool
sudo journalctl -u memoria-warner-bridge --since "10 minutes ago" --no-pager \
  | grep -E 'warner video frames|VIDEO STALLED|p2p|fatal|error'
```

The frame watchdog reports every five seconds, detects an established-stream
stall after 20 seconds, and allows 45 seconds for the first frame. Configure
these with `WARNER_FRAME_STALL_SECONDS` and
`WARNER_FRAME_STARTUP_GRACE_SECONDS` when required.

## 10. Network and security

- Do not expose TCP 8090 directly to the public Internet.
- Restrict it to an administrator LAN, firewall allow-list, or VPN.
- Do not port-forward the admin interface from a router.
- Keep LiveKit API secrets and camera device secrets only in local JSON files.
- Limit file permissions to the service account.
- Rotate any credential that has been pasted into chat, email or logs.
- Use HTTPS through an authenticated reverse proxy if remote administration is
  required.
- Outbound access is required for Warner/Trueview cloud, Nemara backend,
  LiveKit signaling and LiveKit media endpoints.

## 11. Capacity and monitoring

An active camera creates one vendor inbound stream, software decoder and one or
more LiveKit outbound publishers. Measure rather than assume capacity.

Test sequentially with 1, 5, 10 and 20 simultaneously requested cameras. At
each step record CPU, memory, inbound/outbound throughput, connection time,
frame continuity and LiveKit packet loss. Keep sustained CPU below roughly 80%
to preserve recovery headroom.

## 12. Backup, upgrade and recovery

1. Back up `cameras.json` and `warner_cameras.json` securely.
2. Stop the background service/task.
3. Replace application and SDK files without overwriting the two JSON files.
4. Run the platform installer if dependencies changed.
5. Start the service/task and inspect health/logs.
6. Request one test camera before testing the full active set.

Before upgrade, preserve the existing executable files as well as the JSON
configuration. To roll back this release, restore the previous
`warner_bridge.py` and `warner_bridge_static/runtime.js`, then restart the
service/task. Verify package checksums before and after replacement.

If the bridge server is lost, install the same package on another machine,
restore the two protected JSON files, and start the service. Never run both
machines simultaneously with the same assigned LiveKit identities.
