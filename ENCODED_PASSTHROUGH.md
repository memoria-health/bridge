# The SDK already gives us encoded frames (2026-08-20)

## Finding

`ConnectApi.onrecvframeex` — a callback `runtime.js` **already hooks** —
delivers the encoded H.264/H.265 elementary stream for every frame,
before anything decodes it.

Their signature, from `warner_sdk/433.connector.js`:

```js
onrecvframeex = function (sessionKey, frametype, frameData, datalen,
                          channel, width, height, enc, fps, timestamp)
```

And the call site, with only transport headers peeled off:

```js
var T = E.parse_p2p_frame_head(t); t = t.subarray(T.len);   // P2P header
var m = E.parse_live_head(t);      t = t.subarray(m.len);   // live header
if (m.frametype == IFRAME || m.frametype == PFRAME) {
  var v = E.parse_video_param(t),
      g = t.subarray(v.len);                                // ← payload
  q.onrecvframeex(sessionKey, m.frametype, g, g.length,
                  m.channel, v.width, v.height, v.enc, v.fps, T.ts_ms);
}
```

`parse_video_param` fixes the header at 24 bytes and names the codec:

```js
("H265" === t.enc || "H264" === t.enc) && (t.len = 24)
```

So per frame we are handed: the encoded payload, its length, the codec
as an ASCII string, I-frame vs P-frame, width, height, fps, and a
millisecond timestamp.

`runtime.js` currently reads args 1, 5, 6 and 7 from this callback —
frame type and dimensions, for a status heartbeat — and **discards
`frameData`**, using the decoded canvas instead.

## What that means

The current pipeline is:

    P2P payload → WASM decode → canvas → captureStream → re-encode → LiveKit

Every stage after the first is avoidable. `VideoDecoder` is even
deliberately deleted (`new_camera_context`) to force the slow WASM
decoder, because WebCodecs transfers to an OffscreenCanvas and
`captureStream()` then sees nothing in headless. That is a workaround
for a pipeline we do not need.

The pipeline could be:

    P2P payload → LiveKit

The browser is still required — the vendor's cloud/P2P transport and
auth are JS + WASM and there is no native SDK. But it stops being a
video pipeline and becomes a transport shim that forwards bytes.

## Expected saving

Unmeasured, and worth measuring before anyone plans capacity on it. The
removed work per camera is: a WASM H.264/H.265 decoder and its heap, a
1280x720+ canvas, continuous rasterisation, and a full WebRTC encode.
What remains is P2P reassembly and a byte copy.

The current cost is ~800MB and roughly a core per camera. Most of that
is decode and encode.

## How it could be built

Three options, cheapest first:

1. **Ship NALUs to Python, publish there.** Expose a Playwright binding
   (`page.expose_binding`) or a local WebSocket; the page forwards
   `frameData` + `enc` + `ts_ms`; Python republishes via WHIP or an
   encoded track. Keeps all LiveKit logic in one place and out of the
   browser. Highest per-frame IPC cost of the three, but simplest to
   reason about and to fall back from.

2. **Publish encoded from the page** via `RTCRtpScriptTransform` /
   Encoded Transform, feeding NALUs into an outgoing track. No IPC, but
   the encoded-transform API is fiddly and the failure modes are quiet.

3. **Feed a local muxer** — page → ffmpeg/GStreamer → RTMP/WHIP into
   LiveKit Ingress. Most moving parts, but the most conventional, and it
   makes recording and transcoding someone else's problem.

Option 1 first, behind a flag, with the canvas path still available:
a bad frame path shows up as a dead camera, and this is a healthcare
product.

## Open questions

- **Annex-B or AVCC?** Whether NALUs carry `00 00 00 01` start codes or
  length prefixes. One live I-frame settles it; handle both regardless,
  it is a few lines.
- **Are SPS/PPS in-band?** A decoder needs them. If the camera only
  sends them on the first I-frame we must cache and re-inject on every
  new subscriber, or late joiners get nothing.
- **Audio.** The same callback carries audio (`parse_audio_param`,
  `C = t.subarray(y.len)`), so the audio tap can follow the same route.
- **Stability across SDK updates.** This is an internal callback. It is
  documented enough to depend on, but a vendor SDK bump could change it,
  so the canvas path should stay behind a flag rather than be deleted.

## What this changes about the vendor conversation

We no longer need to ask Warner whether the encoded stream is reachable
— it is, and their own source proves it. The remaining ask is a native
Linux SDK, which would drop the browser entirely rather than merely
demoting it. That is a much bigger change for them, so it is now the
only thing worth asking for.
