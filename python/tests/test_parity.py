#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""The Python implementation against spec/fixtures (the Java one runs the same fixtures in
android/test/adshut/core/ParityTest.java). Run: PYTHONPATH=python python3 -m unittest python/tests/test_parity.py
"""
import json
import os
import unittest

import numpy as np

from adshut import fp
from adshut.detect import Detector
from adshut.fingerprint import Fingerprinter, decode
from adshut.screen import Activity, find_screen

FIX = os.path.join(os.path.dirname(__file__), "..", "..", "spec", "fixtures")


def path(name):
    return os.path.join(FIX, name)


def load_json(name):
    with open(path(name), encoding="utf-8") as f:
        return json.load(f)


def read_text(name):
    with open(path(name), encoding="utf-8") as f:
        return f.read()


def templates():
    out = []
    with open(path("templates/index.tsv"), encoding="utf-8") as f:
        lines = f.read().splitlines()
    for line in lines:
        name, kind, thr, frames, channel = line.split("\t")
        zones = np.fromfile(path("templates/%s.zones" % name), dtype=np.uint8).reshape(-1, 9, 16, 3)
        f, w = fp.features(zones)
        out.append(({"name": name, "kind": kind, "threshold": float(thr), "channel": channel}, f, w))
    return out


class Parity(unittest.TestCase):
    def test_features(self):
        stream = np.fromfile(path("stream.bin"), dtype=fp.REC)
        f, w = fp.features(stream["z"][:200])
        np.testing.assert_allclose(f, np.fromfile(path("features.f32"), "<f4").reshape(200, -1), atol=1e-6)
        np.testing.assert_allclose(w, np.fromfile(path("weights.f32"), "<f4"), atol=1e-6)

    def test_detector(self):
        stream = np.fromfile(path("stream.bin"), dtype=fp.REC)
        tpl = templates()
        det = Detector()
        acts = det.set_channel(tpl[0][0]["channel"], tpl, float(stream["t"][0]))
        for t, z in zip(stream["t"], stream["z"]):
            acts += det.push(float(t), z)
        got = ["%.3f\t%s\t%s\t%s\t%s" % (a["t"], a["action"], a.get("template") or "", a.get("kind", ""),
                                         "%.4f" % a["score"] if "score" in a else "") for a in acts]
        self.assertEqual(got, read_text("actions.tsv").splitlines())

    def test_activity(self):
        frames = np.fromfile(path("luma_20x320x180.u8"), dtype=np.uint8).reshape(-1, 180, 320)
        act = Activity()
        for fr in frames:
            act.add(fr)
        np.testing.assert_allclose(act.take(), np.fromfile(path("activity20.f32"), "<f4").reshape(180, 320), atol=1e-4)

    def test_screen(self):
        amap = np.fromfile(path("activity.f32"), "<f4").reshape(180, 320).astype(np.float64)
        corners, _ = find_screen(amap)
        np.testing.assert_allclose(corners, load_json("screen.json")["corners_px"], atol=0.01)

    def test_fingerprint(self):
        cam = load_json("camera.json")
        fpr = Fingerprinter(cam["corners"])
        with open(path("camera.jpg"), "rb") as f:
            zones = fpr.zones(fpr.rectify(decode(f.read())))
        np.testing.assert_array_equal(zones.ravel(), np.fromfile(path("camera.zones"), np.uint8))


if __name__ == "__main__":
    unittest.main()
