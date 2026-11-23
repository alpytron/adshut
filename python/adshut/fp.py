# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Loading recorder data and turning zone fingerprints into features that survive camera changes.

A fingerprint record is (epoch, uint8[9][16][3] zone means of Y, Cb, Cr). Raw values depend on
camera exposure and white balance, so features compare each zone with the rest of its frame:

  luma:   zone Y minus the frame's mean Y, divided by the frame's Y spread (pattern of light and
          dark, invariant to brightness and contrast changes)
  chroma: zone Cb, Cr minus the frame's mean Cb, Cr (pattern of colour, invariant to a white
          balance shift), on a fixed scale

Two frames match when their feature vectors correlate. Frames with almost no spatial structure
(black, single colour) carry little information; `weight` says how much each frame counts.
"""
import json
import os
from datetime import datetime

import numpy as np

# Data directory: $ADSHUT_DATA, default ~/.local/share/adshut
DATA = os.path.expanduser(os.environ.get("ADSHUT_DATA") or "~/.local/share/adshut")
RATE = 5.0                      # timeline grid, frames per second
REC = np.dtype([("t", "<f8"), ("z", "u1", (9, 16, 3))])
CHROMA_SCALE = 1 / 12.0         # chroma deviations of ~12 levels count like one luma std
MIN_SPREAD = 6.0                # luma std (levels) below which a frame is treated as featureless


def load_records(day=None, data=DATA):
    days = [day] if day else sorted(f[:-4] for f in os.listdir(os.path.join(data, "fp")) if f.endswith(".bin"))
    parts = [np.fromfile(os.path.join(data, "fp", d + ".bin"), dtype=REC) for d in days]
    return np.concatenate(parts) if parts else np.zeros(0, REC)


def load_events(data=DATA):
    with open(os.path.join(data, "events.jsonl")) as f:
        return [json.loads(line) for line in f if line.strip()]


def features(z):
    """(n, 9, 16, 3) uint8 -> (n, 432) float32 features and (n,) weights in 0..1."""
    z = z.astype(np.float32).reshape(len(z), -1, 3)
    y = z[:, :, 0]
    ym = y.mean(axis=1, keepdims=True)
    ys = y.std(axis=1, keepdims=True)
    luma = (y - ym) / np.maximum(ys, MIN_SPREAD)
    chroma = (z[:, :, 1:] - z[:, :, 1:].mean(axis=1, keepdims=True)) * CHROMA_SCALE
    f = np.concatenate([luma, chroma.reshape(len(z), -1)], axis=1)
    f /= np.maximum(np.linalg.norm(f, axis=1, keepdims=True), 1e-6)
    weight = np.clip(ys[:, 0] / (2 * MIN_SPREAD), 0, 1)
    return f.astype(np.float32), weight.astype(np.float32)


class Timeline:
    """Fingerprints resampled onto a fixed RATE grid; grid points without a frame are invalid.

    Attributes: t0 (epoch of index 0), feat (n, d), weight (n,), valid (n,) bool, zones (n, 9, 16, 3).
    """

    def __init__(self, records, max_gap=0.6):
        t = records["t"]
        self.t0 = float(t[0]) if len(t) else 0.0
        n = int(np.ceil((t[-1] - self.t0) * RATE)) + 1 if len(t) else 0
        grid = self.t0 + np.arange(n) / RATE
        idx = np.clip(np.searchsorted(t, grid), 1, max(len(t) - 1, 1))
        left = idx - 1
        nearest = np.where(np.abs(t[left] - grid) <= np.abs(t[idx] - grid), left, idx)
        self.valid = np.abs(t[nearest] - grid) <= max_gap / 2 + 0.5 / RATE
        self.zones = records["z"][nearest]
        self.feat, self.weight = features(self.zones)
        self.weight[~self.valid] = 0
        self.feat[~self.valid] = 0

    def __len__(self):
        return len(self.valid)

    def index(self, epoch):
        return int(round((epoch - self.t0) * RATE))

    def time(self, i):
        return self.t0 + i / RATE

    def hms(self, i):
        return datetime.fromtimestamp(self.time(i)).strftime("%H:%M:%S.%f")[:10]
