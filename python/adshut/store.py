# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""The data directory of a host: events, fingerprints and review images.

Layout (shared with the TV app's DataLog and the template server, see spec/):
  events.jsonl               one JSON object per event: {"t": epoch s, "ts": local ISO, "type": ...}
  fp/YYYY-MM-DD.bin          records of float64 LE epoch + uint8[9][16][3] zone means (Y, Cb, Cr)
  thumbs/YYYY-MM-DD/HH.mjpg  concatenated review JPEGs, HH.idx lists "<epoch> <offset> <length>"
"""
import json
import os
import re
import shutil
import struct
import threading
import time
from datetime import datetime, timedelta


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Store:
    def __init__(self, data_dir, grid=(16, 9), rect_size=(320, 180), thumb_keep_days=7):
        self.dir = os.path.expanduser(data_dir)
        self.thumb_keep_days = thumb_keep_days
        os.makedirs(os.path.join(self.dir, "fp"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "thumbs"), exist_ok=True)
        self.lock = threading.Lock()
        self.events = open(os.path.join(self.dir, "events.jsonl"), "a", buffering=1)
        self.fp_file = self.fp_day = None
        self.th_file = self.th_idx = self.th_key = None
        gw, gh = grid
        self.record = struct.Struct("<d%ds" % (gw * gh * 3))
        with open(os.path.join(self.dir, "fp", "format.json"), "w") as f:
            json.dump({
                "record_bytes": self.record.size,
                "layout": "float64 little-endian epoch seconds, then uint8[rows][cols][3]",
                "rows": gh, "cols": gw, "channels": "Y, Cb, Cr (JPEG full range), zone means",
                "corners": "see 'calibration' events in events.jsonl", "rect_size": list(rect_size),
            }, f, indent=1)

    def event(self, kind, t=None, **fields):
        t = t or time.time()
        rec = {"t": round(t, 3), "ts": datetime.fromtimestamp(t).isoformat(timespec="seconds"), "type": kind}
        rec.update(fields)
        with self.lock:
            self.events.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log("event", kind, fields if fields else "")

    def fingerprint(self, t, zones):
        day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
        if day != self.fp_day:
            if self.fp_file:
                self.fp_file.close()
            self.fp_file = open(os.path.join(self.dir, "fp", day + ".bin"), "ab")
            self.fp_day = day
            self.prune()
        self.fp_file.write(self.record.pack(t, zones.tobytes()))
        self.fp_file.flush()

    def thumb(self, t, jpeg):
        d = datetime.fromtimestamp(t)
        key = d.strftime("%Y-%m-%d/%H")
        if key != self.th_key:
            for f in (self.th_file, self.th_idx):
                if f:
                    f.close()
            base = os.path.join(self.dir, "thumbs", key)
            os.makedirs(os.path.dirname(base), exist_ok=True)
            self.th_file = open(base + ".mjpg", "ab")
            self.th_idx = open(base + ".idx", "a", buffering=1)
            self.th_key = key
        off = self.th_file.tell()
        self.th_file.write(jpeg)
        self.th_file.flush()
        self.th_idx.write("%.3f %d %d\n" % (t, off, len(jpeg)))

    def prune(self):
        keep = self.thumb_keep_days
        if keep <= 0:
            return
        limit = (datetime.now() - timedelta(days=keep)).strftime("%Y-%m-%d")
        root = os.path.join(self.dir, "thumbs")
        for name in os.listdir(root):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", name) and name < limit:
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                log("pruned review images of", name)
