# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Exact extents of recurring material, by correlating a window against the recorded history.

The frame-by-frame similarity matrix between a window and the history shows material that aired
before as diagonal runs. A run starts and stops exactly where the two airings stop agreeing, so
an ident followed by different ads each time yields the ident at its true length.

segments() splits the window wherever the set of recurrences covering it changes, so an ident
and the ad after it (which may recur elsewhere on its own) come out as separate segments.
"""
from collections import defaultdict

import numpy as np

from adshut import fp
from adshut import match

FRAME_MATCH = 0.85     # frames this similar count as the same picture
MIN_RUN = 8            # grid frames (1.6 s): shorter agreement is coincidence
MAX_STEP_GAP = 3       # grid frames a run may skip (unsampled or poorly matching frames)
EDGE_TRIM = 1          # run ends jitter by a frame; trimmed before segmenting
MIN_SEGMENT = 8        # grid frames (1.6 s): shortest segment worth a template
USABLE_WEIGHT = 0.5    # featureless frames (black, one colour) all look alike
STATIC_SIM = 0.97      # mean similarity of consecutive frames above which a stretch is a still card


def runs(win_feat, win_ok, tl, hist_ok):
    """Diagonal runs between a window and one timeline: list of (w_start, w_end, h_start)."""
    if not hist_ok.any() or not win_ok.any():
        return []
    sim = win_feat @ tl.feat.T                                   # (window, history)
    sim = match._max_filter(sim, match.SLACK)
    sim[~win_ok] = 0
    sim[:, ~hist_ok] = 0
    i, j = np.nonzero(sim > FRAME_MATCH)
    by_offset = defaultdict(set)
    for a, b in zip(i, j):
        by_offset[b - a].add(int(a))
    out = []
    for d in sorted(by_offset):
        # Neighbouring offsets belong to the same diagonal: sampling jitter shifts it by a step.
        ii = sorted(by_offset[d] | by_offset.get(d - 1, set()) | by_offset.get(d + 1, set()))
        start = prev = ii[0]
        for x in ii[1:] + [None]:
            if x is not None and x - prev <= MAX_STEP_GAP:
                prev = x
                continue
            if prev - start + 1 >= MIN_RUN:
                out.append((start, prev, start + d))
            if x is not None:
                start = prev = x
    # The +-1 offset merge reports a diagonal up to three times; keep the longest of each.
    out.sort(key=lambda r: -(r[1] - r[0]))
    kept = []
    for r in out:
        if not any(abs(r[0] - k[0]) <= 3 and abs(r[2] - k[2]) <= 3 for k in kept):
            kept.append(r)
    return kept


def recurrences(win_feat, win_ok, timelines, exclude):
    """Other airings of window material: list of (w_start, w_end, epoch the airing of w_start began).

    One airing often yields several nearly parallel runs (camera jitter, still frames); runs that
    overlap in both the window and the history are merged into one airing.
    exclude: (t0, t1) epoch span of the history left out (the break the window belongs to).
    """
    found = []
    for tl in timelines:
        if not len(tl):
            continue
        times = tl.t0 + np.arange(len(tl)) / fp.RATE
        ok = tl.valid & (tl.weight >= USABLE_WEIGHT) & ~((times >= exclude[0]) & (times <= exclude[1]))
        for w0, w1, h0 in runs(win_feat, win_ok, tl, ok):
            found.append([w0, w1, float(times[h0])])
    merged = True
    while merged:
        merged = False
        for i in range(len(found)):
            for j in range(i + 1, len(found)):
                a, b = found[i], found[j]
                # history spans of both runs, and the offset between window and history time
                a_h1, b_h1 = a[2] + (a[1] - a[0]) / fp.RATE, b[2] + (b[1] - b[0]) / fp.RATE
                same_air = a[2] <= b_h1 + 1.0 and b[2] <= a_h1 + 1.0
                same_win = a[0] <= b[1] + 3 and b[0] <= a[1] + 3
                if same_air and same_win:
                    w0, w1 = min(a[0], b[0]), max(a[1], b[1])
                    h0 = min(a[2] - a[0] / fp.RATE, b[2] - b[0] / fp.RATE) + w0 / fp.RATE
                    found[i] = [w0, w1, h0]
                    del found[j]
                    merged = True
                    break
            if merged:
                break
    return [tuple(f) for f in found]


def airing_times(found, a, b, min_cover=0.6):
    """Epochs at which the window stretch a..b aired elsewhere as a whole.

    Only airings covering at least min_cover of the stretch count: one that matches just its
    edge (a still card bleeding into an ident) is an airing of the neighbour, not of the stretch.
    """
    out = []
    for w0, w1, h0 in found:
        overlap = min(b, w1) - max(a, w0) + 1
        if overlap >= min_cover * (b - a + 1):
            out.append(h0 + (a - w0) / fp.RATE)
    return sorted(out)


def is_static(feat):
    """True when consecutive frames are nearly identical throughout (a still card)."""
    if len(feat) < 2:
        return True
    return float(np.mean(np.sum(feat[1:] * feat[:-1], axis=1))) > STATIC_SIM


def segments(n, found):
    """Splits window frames 0..n-1 into segments with a constant set of covering recurrences.

    Returns [(start, end, count)] for segments covered by at least one recurrence and at least
    MIN_SEGMENT frames long; count is how many other airings the segment has.
    """
    cover = [frozenset() for _ in range(n)]
    for k, (w0, w1, _) in enumerate(found):
        a, b = w0 + EDGE_TRIM, w1 - EDGE_TRIM
        for i in range(max(a, 0), min(b, n - 1) + 1):
            cover[i] = cover[i] | {k}
    segs = []
    start = 0
    for i in range(1, n + 1):
        if i == n or cover[i] != cover[start]:
            segs.append([start, i - 1, cover[start]])
            start = i
    # Absorb slivers (run-edge jitter) into the neighbour they share most recurrences with.
    changed = True
    while changed:
        changed = False
        for k, (a, b, s) in enumerate(segs):
            if b - a + 1 >= 5 or len(segs) == 1:
                continue
            nb = [j for j in (k - 1, k + 1) if 0 <= j < len(segs)]
            j = max(nb, key=lambda j: len(segs[j][2] & s) + (segs[j][1] - segs[j][0]) / 1000)
            lo, hi = min(segs[j][0], a), max(segs[j][1], b)
            segs[j] = [lo, hi, segs[j][2]]
            del segs[k]
            changed = True
            break
    return [(a, b, len(s)) for a, b, s in segs if s and b - a + 1 >= MIN_SEGMENT]
