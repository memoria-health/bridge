# Release notes

## 2026.08.21 — transport-only browser, and a contract with the backend

The bridge worked before this. The goal was to make it cheap enough and
trustworthy enough to deploy, not to change what it does.

### NOT production ready yet. One blocker, one dependency.

**BLOCKER — one bridge can only ever serve ONE caregiver.** `public.gateways`
binds a token to a single `caregiver_id`, and provisioning does
`UPDATE gateways SET caregiver_id = ...` on that token. So provisioning a
camera for a second customer silently REBINDS the whole bridge to them, and
the first customer's cameras vanish from the roster and stop publishing.

That model is correct for the mini-PC gateway — one box, one home, one
account. The bridge is our own hosted service serving many customers, so it
inherits a rule that does not fit. Nothing here is a partial fix for it; it
needs a decision about whether a bridge holds N credentials, or the backend
learns that a service can serve N accounts.

**DEPENDENCY —** the bridge now polls `POST /v1/fleet/poll`, which exists
only on the backend's `feat/camera-ownership` branch. The backend ships
first or the bridge cannot poll at all.

### What changed

| | before | after |
|---|---|---|
| browser | decoded in WASM, painted a 3MP canvas, re-encoded | forwards the compressed bytes and nothing else |
| decode | WebAssembly, in the browser | native ffmpeg |
| publish | browser WebRTC | Python `rtc.VideoSource`, as it always was over the wire |
| per camera | ~800 MB, ~1 core | ~350 MB, 0.42 cores (measured) |
| publish bitrate | ~6 Mbps | 2 Mbps |
| `desired_state`, `privacy_enabled`, `audio_capture_enabled` | received on every poll, acted on for none | applied on every poll |
| credential | one shared `gateway_token` in `cameras.json` | minted per instance, 0600, revocable with one DELETE |
| layout | `warner_bridge.py`, `warner_cameras.json` | `bridge.py`, `inventory.json`, `core/` + `vendors/` |
| poll | `GET /v1/gateway/roster` | `POST /v1/fleet/poll`, carrying per-camera health |

### Bugs fixed that were live

- `stream_id` defaulted to the **sub stream** server-side, so a 3MP sensor
  published at 800x448. The admin form was fixed in August; three Python
  fallbacks and the validation harness were not.
- `privacy_enabled` allow-listed the literal string `"detector_worker"`,
  which is nobody's participant identity. A private camera was invisible to
  the detector and the identity service as well as to caregivers — the exact
  inverse of the product promise.
- The facilities audio kill passed a track object to `unpublish_track`, which
  wants a SID string. It raised every time.
- A caregiver pressing disconnect changed a database row and nothing else.
- A crashed ffmpeg was never restarted: the camera froze behind a green
  health light until it left the roster.
- Health was measured at the browser, so the decode, the frame socket, the
  LiveKit room and the pump could all be dead while a camera read healthy.
- One camera's failure skipped every camera after it in the plan.
- A failed page load leaked a ~144 MB renderer every 2 seconds.
- Setting `BRIDGE_KEY`, which the deploy unit tells operators to set, took
  every camera on the box dark.
- ffmpeg's memory was mostly ours: a 64 KB pipe limit against 4.5 MB frames
  paused the pipe ~67 times per frame. 239 MB -> 187 MB.
- A fresh install had no ffmpeg at all, so a new box would have run no
  cameras while looking healthy.

### Verified

Four end-to-end runs against a real camera and a real LiveKit: publishes at
2304x1296, privacy applied, audio toggles both ways without dropping the
stream, disconnect stops it, report is per-camera. 130 unit tests.

### NOT verified

Multi-camera. Anything longer than an hour. The ffmpeg restart path against a
real kill. Deployment on a real host.

### Still outstanding, beyond the blocker

- The bridge signs its own LiveKit tokens, so every box holds the project
  API secret. The gateway does not — it asks the backend to mint each token.
  Moving to that removes the secret from the box entirely.
- Enrolment is authorised by an arbitrary bearer string plus a caregiver's
  email address, which is not a secret.
- `src/services/ingest/pi/` in the backend is misnamed: `auth.py` is the
  credential system this bridge depends on, while the MJPEG relay beside it
  is dead.

## 2026.08.19-watchdog.1

- Added a five-second encoded-video frame heartbeat.
- Added automatic detection of silent Warner H.265/WASM decoder freezes.
- Recreates only the affected camera's Chromium worker after 20 seconds without
  frames; the complete bridge service and other camera workers remain running.
- Added a 45-second initial-frame grace period.
- Added `last_frame_at`, `frame_age_seconds`, `frame_count`, `restart_count` and
  `last_restart_at` to worker health output.
- Redacted LiveKit access-token and join-request values from new browser logs.
- Updated Linux and Windows health scripts to detect stale video frames.

Observed production behavior: the vendor decoder may freeze while P2P and
LiveKit connections remain active. This release limits the interruption by
automatically recreating the frozen worker. Vendor SDK investigation remains
recommended.
