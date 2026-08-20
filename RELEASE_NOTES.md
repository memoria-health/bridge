# Release notes

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
