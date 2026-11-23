# adshut formats and protocol

What every implementation (Android app: Java, hosts: Python) must agree on. `fixtures/` holds
frozen inputs with the expected outputs of the Python reference (`python/tests/make_fixtures.py`);
`./tests.sh` checks both implementations against them.

## Fingerprint

A camera frame's screen area, warped to 320x180 and cut into 16 x 9 zones of 20 x 20 pixels;
per zone the mean Y, Cb, Cr (JPEG full range), rounded to bytes. 432 bytes, row-major:
`zones[row][col][Y, Cb, Cr]`. The warp follows Pillow's PERSPECTIVE/BILINEAR conventions
(output pixel centres, input sampled at centre - 0.5, truncation to 8 bits); corners are TL, TR,
BR, BL as fractions of the camera frame. The YUV path (Android) may differ by up to 2 levels per
zone from the JPEG path (hosts). Fixture: `camera.*`.

Record on disk and on the wire: float64 little-endian epoch seconds + the 432 bytes = 440 bytes.
Files `fp/YYYY-MM-DD.bin` (local time). Fixture: `stream.bin`.

## Features (camera-invariant)

For a fingerprint: luma `(Y - mean Y) / max(std Y, 6)` per zone, chroma `(Cb - mean Cb) / 12` and
`(Cr - mean Cr) / 12`; vector = 144 luma values then 144 (Cb, Cr) pairs, L2-normalised (float32).
Weight `clip(std Y / 12, 0, 1)`. Fixture: `features.f32`, `weights.f32`.

## Detector

Frames on a 5 fps grid (slot = round((t - t0) * 5); skipped slots take the nearest frame, two
frames in a slot keep the newer; a gap over 2 s restarts the grid). A template of m frames is
scored at the alignments ending on the newest slot or the one before: each template frame takes
the best correlation within +-1 slot, frames with weight <= 0.3 count -1, and the score is the mean
of the best round(0.6 m) frames. Fires at score >= threshold (default 0.8), once per 15 s per
template. States: not in break -> start template -> in break -> end template (only after 10 s) ->
not in break. Fails open when a break the detector muted lasts 8 min, the channel changes or the
picture stops for 10 s. Fixture: `templates/`, `actions.tsv` (t, action, template, kind, score).

## Screen finder

Activity = mean over frames of |high-pass(frame) - high-pass(previous)|, high-pass = pixel minus
its 5 x 5 mean (edge padded). Threshold at 25% between the 25th and 90th percentile, 3 x 3
majority filter, connected region around the densest spot, holes filled, outline -> Hough lines
-> least-squares polish -> edges moved 2 px (per 320) inwards. Fixture: `luma_20x320x180.u8` ->
`activity20.f32`; `activity.f32` -> `screen.json`.

## Templates on the wire

`index` lines or JSON entries `{name, kind ("start" | "end"), threshold, frames, channel}` plus
`NAME.zones` = frames x 432 bytes (the raw fingerprints; receivers compute features themselves).

## Events (`events.jsonl`)

One JSON object per line: `t` (epoch seconds), `ts` (local ISO time), `type`, and per type:
`power {awake}`, `app {app, activity?}`, `channel {id, number, name}`, `mute {muted, channel,
by_detector?, first?}` (no `by_detector` and no `first` = the user's press: a learning label),
`detector {action: hit|mute|unmute, template, template_kind?, score?, reason?, mode?, channel}`,
`calibration {change, corners, previous?, shift?}`, `capture {on, frames?}`. `first` marks a
state found at start-up rather than a change.

## Template server API

See `python/adshut/server.py`: per-TV bearer tokens; `GET /v1/templates[?channel=]` (ETag),
`GET /v1/templates/NAME.zones`, `GET /v1/sync/TV`, `PUT /v1/sync/TV/fp/DAY?offset=N`,
`PUT /v1/sync/TV/events?offset=N` (resumable: data already stored is skipped).
