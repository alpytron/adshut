#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Generates spec/fixtures from SYNTHETIC inputs with the Python reference implementation.

Everything here is drawn by this script from a fixed random seed: an invented broadcast (a
programme, an opening ident, commercials, a closing ident, the programme again), the templates
of those idents as a learner would have cut them from an earlier airing, and a camera view of an
invented room with a TV in it. No recorded broadcast or camera image is used, so the fixtures
carry no third-party content.

Both test suites (python/tests/test_parity.py, android/test/.../ParityTest.java) must reproduce
the expected outputs. Regenerate only when the reference behaviour changes on purpose:
    PYTHONPATH=python python3 python/tests/make_fixtures.py
"""
import io
import json
import os
import shutil
import sys

import numpy as np
from PIL import Image

from adshut import fp
from adshut.detect import Detector
from adshut.fingerprint import Fingerprinter, decode
from adshut.screen import Activity, find_screen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from synthetic import (CAM_H, CAM_W, CORNERS, QUAD, RATE, camera_frames, ident,  # noqa: E402
                       render, scene, through_camera, zones)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(ROOT, "spec", "fixtures")
CHANNEL = "Example Channel"
T0 = 1767268800.0               # 2026-01-01 12:00 UTC


def broadcast(rng):
    """Zones of the synthetic stretch and the template zones cut from an 'earlier airing'."""
    pics = []
    def shots(seconds, shot_s):
        n = int(seconds * RATE)
        while n > 0:
            sc, m = scene(rng), min(n, int(shot_s * RATE))
            pics.extend(zones(render(sc, k)) for k in range(m))
            n -= m
    shots(40, 6)
    pics.extend(zones(ident(0, k)) for k in range(int(5 * RATE)))
    shots(90, 15)
    pics.extend(zones(ident(1, k)) for k in range(int(4 * RATE)))
    shots(40, 6)
    cam = np.random.default_rng(7)
    stream = np.zeros(len(pics), fp.REC)
    stream["t"] = T0 + np.arange(len(pics)) / RATE + rng.uniform(-0.03, 0.03, len(pics))
    stream["z"] = [through_camera(p, cam, 0.9, 8) for p in pics]
    earlier = np.random.default_rng(11)
    tpls = [("example-start-a", "start", 0.8, [ident(0, k) for k in range(25)]),
            ("example-start-b", "start", 0.85, [ident(0, k) for k in range(5, 20)]),
            ("example-end-a", "end", 0.85, [ident(1, k) for k in range(20)]),
            ("example-start-unaired", "start", 0.85, [ident(2, k) for k in range(20)])]
    tpls = [(n, kind, thr, np.array([through_camera(zones(i), earlier, 1.15, -10) for i in imgs]))
            for n, kind, thr, imgs in tpls]
    return stream, tpls


def main():
    rng = np.random.default_rng(2026)
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(os.path.join(OUT, "templates"))

    stream, tpls = broadcast(rng)
    stream.tofile(os.path.join(OUT, "stream.bin"))

    # 1. features of the first 200 frames of the stream
    f, w = fp.features(stream["z"][:200])
    f.astype("<f4").tofile(os.path.join(OUT, "features.f32"))
    w.astype("<f4").tofile(os.path.join(OUT, "weights.f32"))

    # 2. detector over the stream with a frozen template set
    tpl = []
    with open(os.path.join(OUT, "templates", "index.tsv"), "w", encoding="utf-8") as idx:
        for name, kind, thr, z in tpls:
            z.tofile(os.path.join(OUT, "templates", name + ".zones"))
            idx.write("%s\t%s\t%s\t%d\t%s\n" % (name, kind, thr, len(z), CHANNEL))
            tf, tw = fp.features(z)
            tpl.append(({"name": name, "kind": kind, "threshold": thr, "channel": CHANNEL}, tf, tw))
    det = Detector()
    acts = det.set_channel(CHANNEL, tpl, float(stream["t"][0]))
    for t, z in zip(stream["t"], stream["z"]):
        acts += det.push(float(t), z)
    with open(os.path.join(OUT, "actions.tsv"), "w") as out:
        for a in acts:
            out.write("%.3f\t%s\t%s\t%s\t%s\n" % (a["t"], a["action"], a.get("template") or "",
                                                 a.get("kind", ""), "%.4f" % a["score"] if "score" in a else ""))
    assert [a["action"] for a in acts if a["action"] != "hit"] == ["mute", "unmute"], acts
    assert not any(a.get("template") == "example-start-unaired" for a in acts)

    # 3. screen finder: activity map arithmetic on 20 camera frames; corners from the map of 300
    cams = camera_frames(300, rng)
    frames = np.array([np.asarray(c.convert("L").resize((320, 180), Image.BILINEAR)) for c in cams])
    frames[:20].tofile(os.path.join(OUT, "luma_20x320x180.u8"))
    act = Activity()
    for fr in frames[:20]:
        act.add(fr)
    act.take().astype("<f4").tofile(os.path.join(OUT, "activity20.f32"))
    act = Activity()
    for fr in frames:
        act.add(fr)
    amap = act.take()
    amap.astype("<f4").tofile(os.path.join(OUT, "activity.f32"))
    corners, info = find_screen(amap)
    truth = np.array(QUAD) / 2.0
    assert np.abs(corners - truth).max() < 6, (corners, truth)
    json.dump({"corners_px": np.round(corners, 3).tolist(), "info": info}, open(os.path.join(OUT, "screen.json"), "w"), indent=1)

    # 4. fingerprint of a camera frame: JPEG path (Python) and YUV planes (Java), +-2 levels
    buf = io.BytesIO()
    cams[0].save(buf, "JPEG", quality=85)
    jpeg = buf.getvalue()
    open(os.path.join(OUT, "camera.jpg"), "wb").write(jpeg)
    corners_f = CORNERS
    fpr = Fingerprinter(corners_f)
    zones_ = fpr.zones(fpr.rectify(decode(jpeg)))
    zones_.tofile(os.path.join(OUT, "camera.zones"))
    full = np.asarray(Image.open(io.BytesIO(jpeg)).convert("YCbCr")).astype(np.float32)
    H, W = full.shape[:2]
    full[..., 0].astype(np.uint8).tofile(os.path.join(OUT, "camera_y_%dx%d.u8" % (W, H)))
    for k, n in ((1, "u"), (2, "v")):
        np.rint(full[..., k].reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3))).astype(np.uint8).tofile(
            os.path.join(OUT, "camera_%s_%dx%d.u8" % (n, W // 2, H // 2)))
    json.dump({"corners": corners_f, "width": W, "height": H}, open(os.path.join(OUT, "camera.json"), "w"))
    print("fixtures: %d frames, %d templates, %d actions, corners %s (truth %s)"
          % (len(stream), len(tpl), len(acts), np.round(corners, 1).tolist(), truth.tolist()))
    for a in acts:
        print("  %+.1f s  %s %s %s" % (a["t"] - T0, a["action"], a.get("template") or "", a.get("score", "")))


if __name__ == "__main__":
    main()
