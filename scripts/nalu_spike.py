#!/usr/bin/env python3
"""Answer one question: is `frameData` a usable encoded stream?

Serves the Warner SDK locally and injects a hook on
`ConnectApi.onrecvframeex`. You log in and open the stream in your own
browser; the hook posts the first few frames' HEADER BYTES back here and
this prints what they are.

Deliberately NOT headless and NOT publishing:
  - no LiveKit connection, so it cannot collide with the running bridge
  - the camera is simply viewed, exactly as the Trueview app views it
  - no credentials are stored; you type them into the SDK page as usual

    python3 scripts/nalu_spike.py
    open http://127.0.0.1:8099/

What it decides:
  * Annex-B (00 00 00 01 / 00 00 01) vs AVCC length-prefixed
  * which NAL types arrive, and whether SPS/PPS are in-band on I-frames
    (if they are not, late subscribers get an undecodable stream)
"""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

HERE = Path(__file__).resolve().parent.parent
SDK_DIR = HERE / "vendors" / "warner" / "sdk"

HOOK = """
<script>
(function () {
  var sent = 0;
  fetch("/spike-ping").catch(function () {});

  function tap(sessionKey, frametype, frameData, datalen, channel,
               w, h, enc, fps, ts) {
    try {
      if (sent < 8 && frameData && datalen > 0) {
        sent += 1;
        var head = Array.from(frameData.slice(0, 64));
        fetch("/spike", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            frametype: frametype, datalen: datalen, enc: enc,
            width: w, height: h, fps: fps, ts: ts, head: head
          })
        }).catch(function () {});
      }
    } catch (e) { console.error("[spike] tap", e); }
  }

  // Codec question, answered by the camera itself. getProductObjectModel()
  // returns every settable property with its allowed values, so anything
  // under Venc (video encoder) tells us the current codec AND whether
  // H.264 is even offered. One button, because it needs a live login.
  window.__dumpCodec = async function () {
    try {
      var model = await getProductObjectModel();
      // Send EVERY property name. The codec is in here somewhere and
      // guessing its name is what failed the first time.
      var all = model.map(function (m) { return m.funCode; });
      var hits = model.filter(function (m) {
        var c = (m.funCode || "").toLowerCase();
        return c.indexOf("venc") >= 0 || c.indexOf("codec") >= 0 ||
               c.indexOf("compress") >= 0 || c.indexOf("encode") >= 0 ||
               c.indexOf("video") >= 0 || c.indexOf("stream") >= 0 ||
               c.indexOf("h26") >= 0 || c.indexOf("profile") >= 0;
      }).map(function (m) {
        return {
          name: m.funCode,
          range: (m.funContent && m.funContent.typeSpec &&
                  m.funContent.typeSpec.range) || null
        };
      });
      await fetch("/model", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({count: model.length, venc: hits, all: all})
      });
      console.info("[spike] codec model posted", hits);
    } catch (e) {
      console.error("[spike] dumpCodec failed", e);
      fetch("/model", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({error: String(e)})}).catch(function(){});
    }
  };
  window.addEventListener("load", function () {
    var b = document.createElement("button");
    b.textContent = "DUMP CODEC OPTIONS";
    b.style.cssText = "position:fixed;top:8px;right:8px;z-index:99999;" +
      "padding:10px 14px;background:#c00;color:#fff;border:0;" +
      "border-radius:6px;font-weight:700;cursor:pointer";
    b.onclick = window.__dumpCodec;
    document.body.appendChild(b);
  });

  // The SDK assigns ConnectApi.onrecvframeex ITSELF, after page load.
  // Wrapping the current value just gets overwritten a moment later, so
  // intercept the property instead: whatever the SDK installs, we see
  // every call and still forward to it.
  function install() {
    if (typeof ConnectApi === "undefined" || !ConnectApi) return false;
    var inner = ConnectApi.onrecvframeex;
    try {
      Object.defineProperty(ConnectApi, "onrecvframeex", {
        configurable: true,
        get: function () {
          return function () {
            tap.apply(null, arguments);
            if (typeof inner === "function") {
              return inner.apply(this, arguments);
            }
          };
        },
        set: function (fn) { inner = fn; console.info("[spike] SDK set handler"); }
      });
    } catch (e) { console.error("[spike] defineProperty failed", e); return false; }
    console.info("[spike] intercepting onrecvframeex");
    return true;
  }

  if (!install()) {
    var tries = 0;
    var t = setInterval(function () {
      if (install() || ++tries > 300) clearInterval(t);
    }, 50);
  }
})();
</script>
"""

NAL_H264 = {1: "P/non-IDR", 5: "IDR (keyframe)", 6: "SEI", 7: "SPS", 8: "PPS", 9: "AUD"}
NAL_H265 = {32: "VPS", 33: "SPS", 34: "PPS", 39: "SEI", 19: "IDR", 20: "IDR_N_LP", 1: "TRAIL_R"}


def describe(head: list[int], enc: str) -> list[str]:
    b = bytes(head)
    out = []
    if b[:4] == b"\x00\x00\x00\x01":
        out.append("Annex-B, 4-byte start code")
        off = 4
    elif b[:3] == b"\x00\x00\x01":
        out.append("Annex-B, 3-byte start code")
        off = 3
    else:
        n = int.from_bytes(b[:4], "big")
        out.append(f"NO start code — likely AVCC/HVCC, first length field = {n}")
        off = 4
    if off < len(b):
        if enc.upper() == "H265":
            t = (b[off] >> 1) & 0x3F
            out.append(f"first NAL type {t} ({NAL_H265.get(t, '?')})")
        else:
            t = b[off] & 0x1F
            out.append(f"first NAL type {t} ({NAL_H264.get(t, '?')})")
    # Are SPS/PPS carried in-band? Scan for further start codes.
    types = []
    i = 0
    while i < len(b) - 4:
        if b[i:i + 3] == b"\x00\x00\x01" or b[i:i + 4] == b"\x00\x00\x00\x01":
            j = i + (4 if b[i:i + 4] == b"\x00\x00\x00\x01" else 3)
            if j < len(b):
                types.append((b[j] >> 1) & 0x3F if enc.upper() == "H265"
                             else b[j] & 0x1F)
            i = j
        else:
            i += 1
    if len(types) > 1:
        out.append(f"multiple NALs in this frame: {types}")
    return out


async def spike(request: web.Request) -> web.Response:
    d = await request.json()
    ft = d.get("frametype")
    enc = str(d.get("enc") or "?")
    print(f"\n=== frame  type={ft}  enc={enc}  "
          f"{d.get('width')}x{d.get('height')}@{d.get('fps')}fps  "
          f"len={d.get('datalen')}  ts={d.get('ts')}")
    head = d.get("head") or []
    print("    first 16 bytes:", " ".join(f"{x:02x}" for x in head[:16]))
    for line in describe(head, enc):
        print("    ->", line)
    return web.json_response({"ok": True})


async def model(request: web.Request) -> web.Response:
    d = await request.json()
    if d.get("error"):
        print(f"\n[model] FAILED: {d['error']}")
        return web.json_response({"ok": False})
    print(f"\n=== CAMERA PROPERTY MODEL ({d.get('count')} properties) ===")
    hits = d.get("venc") or []
    if not hits:
        print("  no Venc/codec properties matched — dumping is still useful,")
        print("  the camera may name them differently.")
    for h in hits:
        print(f"  {h['name']}")
        print(f"      allowed: {h['range']}")
    names = d.get("all") or []
    if names:
        print(f"\n--- ALL {len(names)} PROPERTY NAMES ---")
        for n in sorted(names):
            print(f"    {n}")
    return web.json_response({"ok": True})


async def ping(_r: web.Request) -> web.Response:
    print("[spike] page loaded from this harness — hook script is running")
    return web.json_response({"ok": True})


async def index(_r: web.Request) -> web.Response:
    html = (SDK_DIR / "index.html").read_text(encoding="utf-8", errors="replace")
    if "</body>" in html:
        html = html.replace("</body>", HOOK + "</body>")
    else:
        html += HOOK
    return web.Response(text=html, content_type="text/html")


def main() -> None:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_post("/spike", spike)
    app.router.add_get("/spike-ping", ping)
    app.router.add_post("/model", model)
    app.router.add_static("/", SDK_DIR, show_index=False)
    print("open http://127.0.0.1:8099/  — log in, pick the camera, "
          "press the open-stream button")
    web.run_app(app, host="127.0.0.1", port=8099, print=None)


if __name__ == "__main__":
    main()
