# The running bridge is not the committed code

**Open since 2026-08-25. Delete this file when it is resolved.**

## What diverges

`core/livekit_adapters.py` publishes a SIMULCAST LADDER (the committed
state). The bridge process that ran between ~11:15 and the TV Room
deletion was built with `simulcast=False` — one layer, 1920x1080 — and
that build is what produced the only stable hour that camera has had:

    before   collapse to 320x180 every 3-4 minutes
    after    one dip to 1280x720 in 14 minutes, recovered in 5 seconds

The change was reverted in git after Aryaman rightly pointed out that
removing the ladder removes the only mechanism by which a SUBSCRIBER can
buy less bandwidth — which is the opposite of the cost goal. The process
was not restarted, so code and reality parted.

## Why it is being left, deliberately

The ladder is safe once every AI subscriber pins its rung and steps down
when idle. `nemara-gateway` commit 1839301 restored the ladder on that
assumption — and measurement on 2026-08-25 falsified it: identity WAS
pinning and was still downshifted 866 times in one window.

So the order of work is:

1. veto geometry normalised (done — pixel geometry broke on every rung
   change, which is why painted faces came back as people)
2. identity `_LAYER_IDLE` medium -> low
3. backend idle step-down (it has `_request_layer` and never uses it)
4. THEN restart the bridge onto the committed ladder and verify

## What to do

Restart via `scripts/run-bridge.sh` once 2 and 3 are live. That alone
resolves the divergence — the script builds from the committed code.
Confirm afterwards with:

    curl -s localhost:8090/api/health | python -c "import json,sys; \
      print([ (l['frame_width'], l['frame_height']) \
              for s in json.load(sys.stdin)['encoders'].values() for l in s ])"

Three layers means the ladder is back. One means the divergence remains.
