# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Client of the template server (adshut/server.py) for hosts that are not on the server machine,
e.g. a Raspberry Pi: uploads this host's fingerprints and events, and keeps the local template
library equal to the server's set, so the detector and tools work unchanged.

Templates arrive as raw zone fingerprints; they are stored in the library's own format
(index.json + NAME.npz with features, weights and zones), marked origin "server".
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request

import numpy as np

from adshut import fp
from adshut.store import log

RECORD = 8 + 432
CHUNK = 2 * 1024 * 1024


class Sync:
    def __init__(self, url, tv, token, data_dir, period_s=60):
        self.url, self.tv, self.token = url.rstrip("/"), tv, token
        self.data = os.path.expanduser(data_dir)
        self.tpl = os.path.join(self.data, "templates")
        self.period = period_s
        self.version = None
        self.last = "not run yet"
        os.makedirs(self.tpl, exist_ok=True)

    def request(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.url + path, data=body, method=method,
                                     headers=dict({"Authorization": "Bearer " + self.token}, **(headers or {})))
        return urllib.request.urlopen(req, timeout=30)

    def run(self):
        while True:
            try:
                changed = self.templates()
                sent = self.upload()
                self.last = ("templates updated; " if changed else "") + "uploaded %d bytes" % sent
                if changed or sent:
                    log("sync:", self.last)
            except Exception as e:
                self.last = "failed: %r" % e
                log("sync:", self.last)
            time.sleep(self.period)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    # ------------------------------------------------------------ templates

    def templates(self):
        headers = {"If-None-Match": self.version} if self.version else {}
        try:
            with self.request("GET", "/v1/templates", headers=headers) as r:
                idx = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return False
            raise
        index_file = os.path.join(self.tpl, "index.json")
        try:
            with open(index_file) as f:
                entries = json.load(f)
        except (OSError, ValueError):
            entries = []
        local = {e["name"]: e for e in entries}
        keep = [e for e in entries if e.get("origin") != "server"]        # anything made here stays
        for t in idx["templates"]:
            path = os.path.join(self.tpl, t["name"] + ".npz")
            if not (t["name"] in local and os.path.exists(path)):
                with self.request("GET", "/v1/templates/" + urllib.parse.quote(t["name"]) + ".zones") as r:
                    zones = np.frombuffer(r.read(), dtype=np.uint8).reshape(-1, 9, 16, 3)
                feat, weight = fp.features(zones)
                np.savez_compressed(path, feat=feat, weight=weight, zones=zones)
            keep.append({"name": t["name"], "channel": t["channel"], "kind": t["kind"],
                         "frames": t["frames"], "seconds": round(t["frames"] / fp.RATE, 1),
                         "threshold": t["threshold"], "origin": "server", "enabled": True})
        served = {t["name"] for t in idx["templates"]}
        for e in entries:
            if e.get("origin") == "server" and e["name"] not in served:
                try:
                    os.remove(os.path.join(self.tpl, e["name"] + ".npz"))
                except OSError:
                    pass
        with open(index_file + ".tmp", "w") as f:
            json.dump(keep, f, ensure_ascii=False, indent=1)
        os.replace(index_file + ".tmp", index_file)
        self.version = idx["version"]
        return True

    # ------------------------------------------------------------ uploads

    def upload(self):
        tv = urllib.parse.quote(self.tv)
        with self.request("GET", "/v1/sync/" + tv) as r:
            status = json.load(r)
        sent = 0
        fpdir = os.path.join(self.data, "fp")
        for name in sorted(os.listdir(fpdir)):
            if not name.endswith(".bin"):
                continue
            path = os.path.join(fpdir, name)
            size = os.path.getsize(path)
            sent += self.push(path, status["fp"].get(name[:-4], 0), size - size % RECORD,
                              "/v1/sync/%s/fp/%s" % (tv, name[:-4]), RECORD)
        ev = os.path.join(self.data, "events.jsonl")
        if os.path.exists(ev):
            sent += self.push(ev, status["events"], os.path.getsize(ev), "/v1/sync/%s/events" % tv, 1)
        return sent

    def push(self, path, start, end, url, unit):
        sent = 0
        with open(path, "rb") as f:
            while start < end:
                n = min(CHUNK, end - start)
                n -= n % unit
                f.seek(start)
                body = f.read(n)
                with self.request("PUT", "%s?offset=%d" % (url, start), body=body) as r:
                    r.read()
                start += n
                sent += n
        return sent
