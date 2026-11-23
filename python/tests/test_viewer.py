#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Recording viewer: derived views from a synthetic event log and fingerprint file, and the
server's admin-only API. Run: PYTHONPATH=python python3 -m unittest python/tests/test_viewer.py
"""
import json
import os
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

import numpy as np

from adshut import fp, server, viewer

T0 = datetime(2026, 11, 20, 20, 0).timestamp()


def ev(dt, **kw):
    return dict(t=T0 + dt, **kw)


EVENTS = [
    ev(0, type="power", awake=True, first=True),
    ev(0.1, type="mute", muted=False, channel="A", first=True),
    ev(1, type="channel", name="A", number="1"),
    ev(100, type="mute", muted=True, channel="A"),                       # user mutes: missed opening
    ev(400, type="detector", action="unmute", reason="unmute marker", template="a-end"),
    ev(400.2, type="mute", muted=False, channel="A", by_detector=True),
    ev(410, type="mute", muted=True, channel="A"),                       # re-mute: wrong unmute
    ev(500, type="mute", muted=False, channel="A"),
    ev(600, type="detector", action="mute", reason="mute marker", template="a-start"),
    ev(600.2, type="mute", muted=True, channel="A", by_detector=True),
    ev(620, type="mute", muted=False, channel="A"),                      # user undoes: false mute
    ev(700, type="detector", action="mute", reason="mute marker", template="a-start"),
    ev(700.2, type="mute", muted=True, channel="A", by_detector=True),
    ev(1180, type="detector", action="unmute", reason="break too long"),
    ev(1180.2, type="mute", muted=False, channel="A", by_detector=True), # failsafe: missed closing
    ev(1200, type="channel", name="B", number="2"),
    ev(1800, type="power", awake=False),
]


class ViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = os.path.join(cls.tmp.name, "tvs", "tv1")
        os.makedirs(os.path.join(d, "fp"))
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            f.writelines(json.dumps(e) + "\n" for e in EVENTS)
        t = T0 + np.arange(0, 1800, 1 / fp.RATE)
        t = t[(t < T0 + 900) | (t > T0 + 1000)]                         # a 100 s gap, same programme
        rec = np.zeros(len(t), fp.REC)
        rec["t"] = t
        rec["z"] = (np.arange(len(t)) % 200)[:, None, None, None]
        rec.tofile(os.path.join(d, "fp", datetime.fromtimestamp(T0).strftime("%Y-%m-%d") + ".bin"))
        cls.v = viewer.Viewer(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_clients(self):
        c = self.v.clients()
        self.assertEqual([x["id"] for x in c], ["tv1"])
        self.assertAlmostEqual(c[0]["days"][0]["frames"], 9000 - 500, delta=2)
        self.assertIsNone(self.v.sources("../tv1"))
        self.assertIsNone(self.v.sources("nope"))

    def test_spans_and_verdicts(self):
        ov = self.v.overview("tv1")
        by = [(s["started_by"], s["ended_by"]) for s in ov["muted"]]
        self.assertEqual(by, [("user", "detector"), ("user", "user"), ("detector", "user"), ("detector", "failsafe")])
        kinds = [x["kind"] for x in ov["verdicts"]]
        self.assertEqual(kinds, ["missed_opening", "wrong_unmute", "false_mute", "missed_closing"])

    def test_programmes(self):
        progs = self.v.overview("tv1")["programmes"]
        self.assertEqual([p["channel"] for p in progs], ["A", "B"])   # the 100 s gap does not split A
        self.assertAlmostEqual(progs[0]["end"] - T0, 1200, delta=1)
        self.assertEqual(progs[0]["breaks"], 4)
        self.assertEqual(progs[0]["issues"], 4)

    def test_strip_and_frames(self):
        buf = self.v.strip("tv1", T0 + 850, T0 + 1050, 20)
        n = struct.unpack_from("<I", buf)[0]
        cover = np.frombuffer(buf, "<f4", n, 4)
        self.assertEqual(n, 20)
        self.assertGreater(cover[0], 0.9)
        self.assertEqual(cover[10], 0)                                  # inside the gap
        self.assertEqual(len(buf), 4 + 16 * n + 432 * n)
        frames = self.v.frames("tv1", T0 + 10, T0 + 20)
        self.assertEqual(len(frames), 50 * 440)


class AdminApiTest(unittest.TestCase):
    def test_admin_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "tvs", "tv1", "fp"))
            store = server.Store(tmp)
            tv_token = store.add_tv("tv1")
            admin_token = store.add_tv("me", "admins.json")
            server.Handler.store, server.Handler.viewer = store, viewer.Viewer(tmp)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % srv.server_address[1]

            def get(path, token=None):
                req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token} if token else {})
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, r.read()
                except urllib.error.HTTPError as e:
                    return e.code, e.read()
            try:
                self.assertEqual(get("/ui/")[0], 200)
                self.assertEqual(get("/v1/admin/clients")[0], 401)
                self.assertEqual(get("/v1/admin/clients", tv_token)[0], 401)
                code, body = get("/v1/admin/clients", admin_token)
                self.assertEqual((code, json.loads(body)[0]["id"]), (200, "tv1"))
                self.assertEqual(get("/v1/admin/c/x/overview", admin_token)[0], 404)
                self.assertEqual(get("/v1/admin/c/tv1/strip?from=a", admin_token)[0], 400)
                self.assertEqual(get("/v1/sync/tv1", admin_token)[0], 401)      # admins are not TVs
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
