/* global Player, ConnectApi */
/*
 * Transport only. The browser's ONE job is the vendor's P2P transport
 * and auth, which are JavaScript and WebAssembly with no native SDK.
 * Everything else it used to do — WASM decode, a 3MP canvas,
 * captureStream, a WebRTC encode, LiveKit rooms, tokens, privacy and
 * the audio kill — is gone. Python owns all of it now.
 *
 *   SDK P2P --onrecvframeex--> this page --WebSocket--> ffmpeg -> LiveKit
 *
 * `Player.init` is deliberately NOT called. It is what fills play.js's
 * `playerList`, and the decode/paint path at play.js:288 is guarded on
 * that list being populated — so leaving it empty skips the WASM
 * decoder and the canvas entirely while `ConnectApi.onrecvframeex`,
 * which fires upstream in the connector, still delivers every frame.
 *
 * Two consequences of that, both deliberate:
 *   - `Player.OpenStream` is replaced by `ConnectApi.open_stream`. The
 *     Player wrapper ends in an UNGUARDED `playerList[winindex].open()`.
 *   - `Player.OpenAudio` is dropped. It sets local speaker volume and
 *     `isSound`, and `isSound` appears nowhere in the connector — the
 *     camera sends audio over P2P regardless.
 */
(async function () {
  const cameraId = new URLSearchParams(location.search).get("camera_id");
  if (!cameraId) throw new Error("camera_id is required");
  const apiBase = `/api/runtime/${encodeURIComponent(cameraId)}`;
  const config = await fetch(`${apiBase}/config`).then(checkJson);

  // The vendor's own constants (433.connector.js).
  const FRAME_AUDIO = 0;
  const FRAME_KEY = 1;
  const FRAME_DELTA = 2;

  let socket = null;
  let dropped = 0;
  let videoFrames = 0;
  let lastFrameReportAt = 0;

  async function checkJson(response) {
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  async function report(state, detail = "", extra = {}) {
    try {
      await fetch(`${apiBase}/status`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ state, detail, ...extra })
      });
    } catch (_) { /* status is best-effort; never block the frame path */ }
  }

  // ── the pipe to Python ─────────────────────────────────────────
  function openSocket() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${proto}//${location.host}${apiBase}/frames`);
    socket.binaryType = "arraybuffer";
    socket.onopen = () => report("frames_open", config.enc || "");
    socket.onerror = () => report("frames_error");
    socket.onclose = () => {
      socket = null;
      report("frames_closed");
      // The bridge restarting must not leave a page happily receiving
      // frames and throwing them away.
      window.setTimeout(openSocket, 1000);
    };
  }

  function forward(frameType, frameData) {
    if (!socket || socket.readyState !== 1) return;
    // Never let a stalled socket become unbounded browser memory. Drop
    // the newest instead and say so — that is the failure the identity
    // OOM already cost a day.
    if (socket.bufferedAmount > 4 * 1024 * 1024) {
      if ((dropped++ % 100) === 0) {
        report("frames_backpressure", `dropped=${dropped}`);
      }
      return;
    }
    // 1-byte header: 0 audio, 1 keyframe, 2 delta. ffmpeg only needs
    // the elementary-stream bytes, but knowing which is which lets
    // Python refuse to start mid-GOP, where a decoder emits garbage.
    const body = new Uint8Array(frameData.byteLength + 1);
    body[0] = frameType;
    body.set(new Uint8Array(
      frameData.buffer || frameData, frameData.byteOffset || 0,
      frameData.byteLength), 1);
    socket.send(body);
  }

  // ── the tap ────────────────────────────────────────────────────
  // Defined as a PROPERTY rather than assigned, because the SDK assigns
  // `onrecvframeex` itself after the page loads; a plain assignment is
  // silently replaced and the stream goes dark. This keeps ours in
  // front and chains to whatever the SDK installs.
  let vendorHandler = ConnectApi.onrecvframeex;
  function tap(...args) {
    const frameType = Number(args[1]);
    const payload = args[2];
    if (frameType === FRAME_KEY || frameType === FRAME_DELTA) {
      forward(frameType, payload);
      videoFrames += 1;
      const now = Date.now();
      if (videoFrames === 1 || now - lastFrameReportAt >= 5000) {
        lastFrameReportAt = now;
        // args 5,6,7 are width, height, codec for a video frame.
        report("frame_heartbeat", `${args[5]}x${args[6]} ${args[7]}`,
               { frame_count: videoFrames });
      }
    } else if (frameType === FRAME_AUDIO) {
      // Same callback, different meaning for the trailing args
      // (samplerate, samplewidth, enc, channels). We only need bytes.
      forward(FRAME_AUDIO, payload);
    }
    if (typeof vendorHandler === "function") {
      return vendorHandler.apply(this, args);
    }
  }
  Object.defineProperty(ConnectApi, "onrecvframeex", {
    configurable: true,
    get() { return tap; },
    set(fn) { vendorHandler = fn; },
  });

  // ── camera session ─────────────────────────────────────────────
  const previousLogin = ConnectApi.onloginresult;
  ConnectApi.onloginresult = function (session, result) {
    if (typeof previousLogin === "function") previousLogin(session, result);
    if (result === 0) {
      // NOT Player.OpenStream — see the header.
      ConnectApi.open_stream(session, config.channel, config.stream_id);
      report("camera_connected", config.device_id);
    } else {
      report("camera_login_failed", String(result));
    }
  };
  ConnectApi.ondisconnect = function (_session, code) {
    report("camera_disconnected", String(code || ""));
    window.setTimeout(() => location.reload(), 2000);
  };
  ConnectApi.onp2perror = function (_session, code) {
    report("camera_p2p_error", String(code || ""));
    window.setTimeout(() => location.reload(), 2000);
  };

  openSocket();
  report("camera_connecting", config.device_id);
  Player.ConnectDevice(
    config.device_key, "", config.camera_user, config.camera_password,
    0, 80, 0, config.channel, config.stream_id, "wss", null
  );
})().catch(error => {
  console.error("Warner runtime fatal", error);
  document.body.dataset.error = error.message;
});
