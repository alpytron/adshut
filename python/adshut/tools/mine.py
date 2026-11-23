#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Finds stretches of picture that occur more than once in the recording.

Repeats are what this whole approach relies on: idents and station cards recur every break,
and ads recur across breaks, while programme content almost never does. Frame pairs that
correlate above FRAME_MATCH form short diagonal runs in the time-vs-time similarity matrix;
runs at least MIN_LEN long are reported, merged when they overlap.

    mine.py [DAY] [--min-len SECONDS] [--json OUT]
"""
import argparse
import json
from collections import defaultdict

import numpy as np

from adshut import fp

FRAME_MATCH = 0.85
MIN_APART_S = 30          # a repeat must be at least this far from the original
MAX_STEP_GAP = 3          # grid steps a run may skip (unsampled or poorly matching frames)


def frame_pairs(tl, chunk=1500):
    """(i, j) grid index pairs, i < j - apart, whose frames correlate above FRAME_MATCH."""
    usable = np.nonzero(tl.valid & (tl.weight >= 0.5))[0]       # featureless frames all look alike
    f = tl.feat[usable]
    apart = int(MIN_APART_S * fp.RATE)
    out = []
    for a in range(0, len(usable), chunk):
        c = f[a:a + chunk] @ f.T
        r, k = np.nonzero(c > FRAME_MATCH)
        i, j = usable[a + r], usable[k]
        keep = j - i > apart
        out.append(np.stack([i[keep], j[keep]], axis=1))
    return np.concatenate(out) if out else np.zeros((0, 2), int)


def runs(pairs, min_len):
    """Diagonal runs: dicts with a (first occurrence start, end) and b (repeat start, end) grid indices."""
    by_offset = defaultdict(list)
    for i, j in pairs:
        by_offset[j - i].append(i)
    found = []
    offsets = sorted(by_offset)
    for d in offsets:
        # Neighbouring offsets belong to the same diagonal: sampling jitter shifts it by a step.
        ii = sorted(set(by_offset[d]) | set(by_offset.get(d - 1, [])) | set(by_offset.get(d + 1, [])))
        start = prev = ii[0]
        for i in ii[1:] + [None]:
            if i is not None and i - prev <= MAX_STEP_GAP:
                prev = i
                continue
            if prev - start >= min_len:
                found.append({"a": (start, prev), "b": (start + d, prev + d)})
            if i is not None:
                start = prev = i
    # The +-1 offset smoothing reports each diagonal up to three times; keep the longest.
    found.sort(key=lambda r: -(r["a"][1] - r["a"][0]))
    kept = []
    for r in found:
        if not any(abs(r["a"][0] - k["a"][0]) < 10 and abs(r["b"][0] - k["b"][0]) < 10 for k in kept):
            kept.append(r)
    return sorted(kept, key=lambda r: r["a"][0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day", nargs="?")
    ap.add_argument("--min-len", type=float, default=2.0)
    ap.add_argument("--json")
    args = ap.parse_args()
    tl = fp.Timeline(fp.load_records(args.day))
    pairs = frame_pairs(tl)
    found = runs(pairs, int(args.min_len * fp.RATE))
    print("%d frame pairs, %d repeated stretches of %.0f s or more" % (len(pairs), len(found), args.min_len))
    for r in found:
        print("  %s-%s  again at %s-%s  (%.1f s)" % (tl.hms(r["a"][0]), tl.hms(r["a"][1]), tl.hms(r["b"][0]),
                                                    tl.hms(r["b"][1]), (r["a"][1] - r["a"][0]) / fp.RATE))
    if args.json:
        with open(args.json, "w") as f:
            json.dump([{"a": [tl.time(x) for x in r["a"]], "b": [tl.time(x) for x in r["b"]]} for r in found], f)


if __name__ == "__main__":
    main()
