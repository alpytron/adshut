# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Finding a short reference sequence (an ident) in a timeline of frame features.

The camera samples the 25 fps broadcast at ~5 fps with no fixed phase, so the same ident never
yields exactly the same frames twice. Matching therefore lets each reference frame pick the best
of the neighbouring timeline frames (+-SLACK grid steps) and averages those correlations along
the sequence, weighting frames by how much structure they have.
"""
import numpy as np

SLACK = 1
KEEP = 0.6        # robust score: mean of the best 60% of frames (simulated 2026-11-22: catches idents
                  # whose first second is a wipe over different material, no new false matches)
WEIGHT_MIN = 0.3  # featureless template frames never count as good ones


def _max_filter(a, r):
    """Max over +-r along the last axis."""
    if r <= 0:
        return a
    out = a.copy()
    for s in range(1, r + 1):
        out[..., s:] = np.maximum(out[..., s:], a[..., :-s])
        out[..., :-s] = np.maximum(out[..., :-s], a[..., s:])
    return out


def scores(ref_feat, ref_weight, tl_feat, tl_valid, slack=SLACK):
    """Score for the reference starting at every timeline index (higher = better, max 1).

    Returns an array of len(timeline); positions where the reference would run off the end, or
    where too few aligned timeline frames are valid, get -1.
    """
    m, n = len(ref_feat), len(tl_feat)
    out = np.full(n, -1.0, dtype=np.float32)
    if n < m:
        return out
    corr = _max_filter(ref_feat @ tl_feat.T, slack)          # (m, n)
    corr[:, ~tl_valid] = 0
    w = ref_weight / max(ref_weight.sum(), 1e-6)
    acc = np.zeros(n - m + 1, dtype=np.float32)
    cover = np.zeros(n - m + 1, dtype=np.float32)
    valid = tl_valid.astype(np.float32)
    for i in range(m):
        acc += w[i] * corr[i, i:i + n - m + 1]
        cover += w[i] * valid[i:i + n - m + 1]
    ok = cover > 0.8
    out[:n - m + 1][ok] = acc[ok] / cover[ok]
    return out


def peaks(score, threshold, min_distance):
    """Indices of local maxima above threshold, at least min_distance apart (best first)."""
    order = np.argsort(score)[::-1]
    taken = []
    for i in order:
        if score[i] < threshold:
            break
        if all(abs(i - j) >= min_distance for j in taken):
            taken.append(int(i))
    return taken


def robust_scores(tf, tw, feat, valid, keep=KEEP):
    """Like scores(), but each alignment is scored on its best `keep` fraction of frames.

    An ident's first second is often a wipe over whatever aired before, and fast motion is
    caught at different moments by the 5 fps sampling; those frames should not sink a match.
    """
    m, n = len(tf), len(feat)
    out = np.full(n, -1.0, dtype=np.float32)
    if n < m:
        return out
    corr = _max_filter(tf @ feat.T, SLACK)
    corr[:, ~valid] = 0
    corr[tw <= WEIGHT_MIN] = -1.0
    k = max(1, int(round(keep * m)))
    idx = np.arange(m)[:, None] + np.arange(n - m + 1)[None, :]
    diag = corr[np.arange(m)[:, None], idx]                       # (m, positions)
    out[:n - m + 1] = (-np.partition(-diag, k - 1, axis=0)[:k]).mean(axis=0)
    return out
