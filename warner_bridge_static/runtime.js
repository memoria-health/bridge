/* global LivekitClient, Player, ConnectApi, JAaudioplay */
(async function () {
  const cameraId = new URLSearchParams(location.search).get("camera_id");
  if (!cameraId) throw new Error("camera_id is required");
  const apiBase = `/api/runtime/${encodeURIComponent(cameraId)}`;
  const config = await fetch(`${apiBase}/config`).then(checkJson);
  const canvas = document.getElementById("canvas1");
  const publishers = new Map();
  const connecting = new Set();
  let reconcileRunning = false;
  let videoStream;
  let audioContext;
  let audioDestination;
  let originalAudioFeed;
  let nextAudioTime = 0;
  let cameraReady = false;
  let lastFrameReportAt = 0;
  let receivedVideoFrames = 0;

  async function checkJson(response) {
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  async function report(state, detail = "") {
    try {
      await fetch(`${apiBase}/status`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ state, detail })
      });
    } catch (_) { /* status is best-effort */ }
  }

  async function reportFrame(frameCount, width, height, encoding) {
    try {
      await fetch(`${apiBase}/status`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          state: "frame_heartbeat",
          detail: `${width}x${height} ${encoding}`,
          frame_count: frameCount
        })
      });
    } catch (_) { /* frame health is best-effort */ }
  }

  function startAudioTap() {
    const pcmPlayer = window.JAaudioplay && window.JAaudioplay.pcmPlayer;
    if (!pcmPlayer || typeof pcmPlayer.feed !== "function") {
      throw new Error("Warner PCM output unavailable");
    }
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    audioContext = new AudioContextClass({ sampleRate: 8000, latencyHint: "interactive" });
    audioDestination = audioContext.createMediaStreamDestination();
    originalAudioFeed = pcmPlayer.feed;
    nextAudioTime = audioContext.currentTime + 0.05;
    pcmPlayer.feed = function (samples) {
      originalAudioFeed.call(pcmPlayer, samples);
      if (!audioContext || audioContext.state === "closed") return;
      const values = new Float32Array(samples);
      const buffer = audioContext.createBuffer(1, values.length, 8000);
      buffer.copyToChannel(values, 0);
      const source = audioContext.createBufferSource();
      source.buffer = buffer; source.connect(audioDestination);
      const now = audioContext.currentTime;
      if (nextAudioTime < now || nextAudioTime > now + 0.5) nextAudioTime = now + 0.02;
      source.start(nextAudioTime); nextAudioTime += buffer.duration;
      source.onended = () => source.disconnect();
    };
    audioContext.resume();
  }

  async function connectPublisher(spec) {
    if (!cameraReady || publishers.has(spec.identity) || connecting.has(spec.identity)) return;
    connecting.add(spec.identity);
    let room;
    let videoTrack;
    let audioTrack;
    try {
      await report("livekit_connecting", `${spec.identity} -> ${spec.room}`);
      const auth = await fetch(`${apiBase}/token`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ identity: spec.identity, room: spec.room })
      }).then(checkJson);
      room = new LivekitClient.Room({ adaptiveStream: true, dynacast: true });
      room.on(LivekitClient.RoomEvent.Disconnected, () => {
        const current = publishers.get(spec.identity);
        if (current && current.room === room) {
          publishers.delete(spec.identity);
          current.videoTrack.stop();
          current.audioTrack.stop();
          report("livekit_disconnected", `${spec.identity} ${spec.room}`);
        }
      });
      await room.connect(auth.url, auth.token);
      const videoMedia = videoStream.getVideoTracks()[0].clone();
      videoTrack = new LivekitClient.LocalVideoTrack(videoMedia);
      // Without an explicit bitrate LiveKit picks a conservative default
      // for the track, which quietly undoes a higher capture resolution:
      // more pixels at the same bitrate is just softer video. Scale the
      // ceiling with the actual pixel count.
      const px = (canvas.width || 1280) * (canvas.height || 720);
      const maxBitrate = Math.round(
        Math.min(8_000_000, Math.max(1_500_000, px * 2.0)));
      await room.localParticipant.publishTrack(videoTrack, {
        name: "warner-camera", source: LivekitClient.Track.Source.Camera,
        simulcast: false,
        videoEncoding: { maxBitrate, maxFramerate: 25 }
      });
      // THE FACILITIES AUDIO KILL. `audio_capture_enabled=false` means
      // this site does not permit recording audio. The backend has sent
      // this on every roster poll since 2026-07-28 and described it as
      // "severed at the source"; we were the source and we published
      // anyway. Not publishing the track at all is the severing — a
      // muted track still opens a microphone path.
      if (spec.audio_capture_enabled) {
        const audioMedia = audioDestination.stream.getAudioTracks()[0].clone();
        audioTrack = new LivekitClient.LocalAudioTrack(audioMedia);
        await room.localParticipant.publishTrack(audioTrack, {
          name: "warner-camera-audio", source: LivekitClient.Track.Source.Microphone
        });
      } else {
        await report("audio_suppressed", `${spec.identity} facilities kill`);
      }

      // BATHROOM/BEDROOM PRIVACY. A private camera is for the AI, not
      // for people: restrict subscription to detector_worker_* so the
      // caregiver app cannot pull the track even though it may join the
      // room. Enforced publisher-side, which is the only place it
      // cannot be argued with.
      if (spec.privacy_enabled) {
        try {
          await room.localParticipant.setTrackSubscriptionPermissions(false, [
            { participantIdentity: "detector_worker", allTracksAllowed: true }
          ]);
          await report("privacy_restricted", spec.identity);
        } catch (e) {
          // Failing open here would publish a bathroom camera to every
          // subscriber in the room. Tear the publisher down instead.
          await report("privacy_enforce_failed", String(e));
          throw e;
        }
      }
      publishers.set(spec.identity, { room, videoTrack, audioTrack, roomName: spec.room });
      await report("publishing", `${spec.identity} -> ${spec.room}`);
    } catch (error) {
      try { if (room) await room.disconnect(); } catch (_) { /* connection never completed */ }
      if (videoTrack) videoTrack.stop();
      if (audioTrack) audioTrack.stop();
      throw error;
    } finally {
      connecting.delete(spec.identity);
    }
  }

  async function disconnectPublisher(identity) {
    const publisher = publishers.get(identity);
    if (!publisher) return;
    publishers.delete(identity);
    try { await publisher.room.disconnect(); } catch (_) { /* already gone */ }
    publisher.videoTrack.stop(); publisher.audioTrack.stop();
  }

  async function reconcile() {
    if (reconcileRunning) return;
    reconcileRunning = true;
    try {
      const desired = await fetch(`${apiBase}/desired`).then(checkJson);
      const wanted = new Map(desired.publishers
        .filter(item => item.desired_state === "connected")
        .map(item => [item.identity, item]));
      for (const identity of [...publishers.keys()]) {
        if (!wanted.has(identity)) await disconnectPublisher(identity);
      }
      for (const item of wanted.values()) {
        if (!publishers.has(item.identity)) await connectPublisher(item);
      }
    } catch (error) {
      console.error("roster reconcile", error);
      await report("reconcile_error", error.message);
    } finally {
      reconcileRunning = false;
    }
  }

  // Capture at the configured size, not the 1280x720 the markup used to
  // hardcode. These are 3MP cameras and we were downscaling to 720p
  // before LiveKit ever saw a pixel — irrecoverable detail, thrown away
  // in the one place nothing downstream can undo it.
  if (config.capture_width && config.capture_height) {
    canvas.width = config.capture_width;
    canvas.height = config.capture_height;
  }

  // ── Encoded passthrough ────────────────────────────────────────
  // The vendor hands us the encoded elementary stream on every frame
  // (measured 2026-08-20: H.265, Annex-B, VPS+SPS in-band on
  // keyframes, ~190 Kbps). Forward those bytes and the decode, the
  // canvas and the re-encode all disappear — that is where the ~800MB
  // and the CPU core per camera go.
  //
  // Off by default. When off, not a single byte of the path below
  // changes, so the canvas route stays exactly as it shipped.
  let naluSocket = null;
  let naluDropped = 0;
  if (config.passthrough) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    naluSocket = new WebSocket(
      `${proto}//${location.host}${apiBase}/nalu`);
    naluSocket.binaryType = "arraybuffer";
    naluSocket.onopen = () => report("passthrough_open", config.enc || "");
    naluSocket.onclose = () => { naluSocket = null; report("passthrough_closed"); };
    naluSocket.onerror = () => report("passthrough_error");
  }

  function forwardNalu(frameType, frameData) {
    if (!naluSocket || naluSocket.readyState !== 1) return;
    // Never let a stalled socket become unbounded browser memory —
    // that is the failure the OOM work spent a day on. Drop instead,
    // and say so.
    if (naluSocket.bufferedAmount > 4 * 1024 * 1024) {
      if ((naluDropped++ % 100) === 0) {
        report("passthrough_backpressure", `dropped=${naluDropped}`);
      }
      return;
    }
    // 1-byte header: 1 = keyframe, 2 = delta. ffmpeg only needs the
    // Annex-B bytes, but knowing which is which lets Python refuse to
    // start a stream mid-GOP, where a decoder would produce garbage.
    const body = new Uint8Array(frameData.byteLength + 1);
    body[0] = frameType;
    body.set(frameData, 1);
    naluSocket.send(body);
  }

  Player.init([canvas]);
  startAudioTap();
  // Capture at the camera's real rate. These stream 15fps (measured
  // 2026-08-20), so 25 just duplicated frames for the encoder to
  // discard. Raised to the true rate on the first frame below.
  videoStream = canvas.captureStream(config.capture_fps || 15);
  const previousFrame = ConnectApi.onrecvframeex;
  ConnectApi.onrecvframeex = function (...args) {
    const frameType = Number(args[1]);
    if (frameType === 1 || frameType === 2) {
      // Forward BEFORE anything else: this is the cheap path and it
      // must not inherit the cost of the expensive one.
      forwardNalu(frameType, args[2]);
      receivedVideoFrames += 1;
      const now = Date.now();
      // Adopt the camera's OWN frame size on the first frame, so a
      // camera that streams something other than the configured guess
      // is published at its native resolution rather than stretched or
      // cropped to ours.
      if (receivedVideoFrames === 1) {
        const w = Number(args[5]); const h = Number(args[6]);
        if (w > 0 && h > 0 && (canvas.width !== w || canvas.height !== h)) {
          console.info(`warner canvas -> native ${w}x${h}`);
          canvas.width = w;
          canvas.height = h;
        }
      }
      if (receivedVideoFrames === 1 || now - lastFrameReportAt >= 5000) {
        lastFrameReportAt = now;
        reportFrame(receivedVideoFrames, args[5], args[6], args[7]);
      }
      if (receivedVideoFrames === 1 || receivedVideoFrames % 250 === 0) {
        const width = args[5]; const height = args[6]; const encoding = args[7];
        console.info(`warner video frames: ${receivedVideoFrames} ${width}x${height} ${encoding}`);
      }
    }
    if (typeof previousFrame === "function") return previousFrame.apply(this, args);
  };
  const previousLogin = ConnectApi.onloginresult;
  ConnectApi.onloginresult = function (session, result) {
    previousLogin(session, result);
    if (result === 0) {
      Player.OpenStream(config.device_id, "", config.channel, config.stream_id, 0);
      Player.OpenAudio(0);
      cameraReady = true;
      report("camera_connected", config.device_id);
      reconcile();
    } else {
      report("camera_login_failed", String(result));
    }
  };
  ConnectApi.ondisconnect = function (_session, code) {
    cameraReady = false;
    report("camera_disconnected", String(code || ""));
    window.setTimeout(() => location.reload(), 2000);
  };
  ConnectApi.onp2perror = function (_session, code) {
    cameraReady = false;
    report("camera_p2p_error", String(code || ""));
    window.setTimeout(() => location.reload(), 2000);
  };

  function connectCamera() {
    report("camera_connecting", config.device_id);
    Player.ConnectDevice(
      config.device_key, "", config.camera_user, config.camera_password,
      0, 80, 0, config.channel, config.stream_id, "wss", null
    );
  }

  connectCamera();
  window.setInterval(reconcile, 1000);
})().catch(error => {
  console.error("Warner runtime fatal", error);
  document.body.dataset.error = error.message;
});
