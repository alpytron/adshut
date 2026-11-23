# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Finds the TV screen in the camera frame from how much each pixel changes over time.

The picture is the only region whose fine detail changes from frame to frame. Raw brightness is
no good for this: the TV lights the room, so walls flicker along with the picture. Activity is
therefore measured on a high-passed image (pixel minus its neighbourhood mean), where smooth
lighting changes cancel out; averaged over a minute it is a bright quadrilateral on a dark
background. find_screen() thresholds that map and fits four straight edges. numpy only.

Usage: feed luma frames to Activity.add(), then pass Activity.take() to find_screen().
"""
import numpy as np


class NoScreen(Exception):
    """The activity map does not contain a usable screen outline; the message says why."""


def _box(a, r):
    """Mean over a (2r+1)^2 window, edges padded."""
    p = np.pad(a.astype(float), r, mode="edge")
    c = np.cumsum(np.cumsum(p, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)))
    k = 2 * r + 1
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def _blob(mask):
    """Connected region of mask around its densest spot (4-connectivity)."""
    dense = _box(mask, 8)
    seed = np.zeros_like(mask)
    seed[np.unravel_index(np.argmax(dense), mask.shape)] = True
    seed &= mask
    if not seed.any():
        return seed
    while True:
        grown = seed.copy()
        grown[1:] |= seed[:-1]
        grown[:-1] |= seed[1:]
        grown[:, 1:] |= seed[:, :-1]
        grown[:, :-1] |= seed[:, 1:]
        grown &= mask
        if (grown == seed).all():
            return seed
        seed = grown


def _fill_holes(blob):
    """blob plus every region of non-blob pixels that does not reach the image border (4-connectivity).

    Static things inside the picture (a channel logo, the setup screen's central panel, flat
    pattern cells) leave holes; their outlines must not compete with the screen's outer edges.
    """
    outside = np.zeros_like(blob)
    outside[0, :], outside[-1, :], outside[:, 0], outside[:, -1] = ~blob[0, :], ~blob[-1, :], ~blob[:, 0], ~blob[:, -1]
    free = ~blob
    while True:
        grown = outside.copy()
        grown[1:] |= outside[:-1]
        grown[:-1] |= outside[1:]
        grown[:, 1:] |= outside[:, :-1]
        grown[:, :-1] |= outside[:, 1:]
        grown &= free
        if (grown == outside).all():
            return ~outside
        outside = grown


def _hough_lines(pts, shape, count=4):
    """The `count` strongest straight lines through the outline points, as (theta, rho, votes).

    A line is x*cos(theta) + y*sin(theta) = rho. Voting makes this indifferent to notches and
    bumps in the outline, which only cost a few votes.
    """
    h, w = shape
    diag = int(np.ceil(np.hypot(h, w)))
    thetas = np.deg2rad(np.arange(180))
    rho = pts[:, :1] * np.cos(thetas) + pts[:, 1:] * np.sin(thetas)
    acc = np.zeros((180, 2 * diag + 1))
    np.add.at(acc, (np.broadcast_to(np.arange(180), rho.shape), np.rint(rho).astype(int) + diag), 1)
    acc[:, 1:-1] = acc[:, :-2] + acc[:, 1:-1] + acc[:, 2:]     # a pixel staircase straddles two bins
    lines = []
    for _ in range(count):
        ti, ri = np.unravel_index(np.argmax(acc), acc.shape)
        lines.append((thetas[ti], ri - diag, acc[ti, ri]))
        for dt in range(-12, 13):                               # clear the peak and its near-duplicates
            t, r = ti + dt, ri
            if not 0 <= t < 180:                                # theta wraps with rho mirrored
                t, r = t % 180, 2 * diag - ri
            acc[t, max(0, r - 10):r + 11] = 0
    return lines


def _fit_line(points):
    """Total-least-squares line through points: (point on line, unit direction)."""
    c = points.mean(axis=0)
    _, _, vt = np.linalg.svd(points - c)
    return c, vt[0]


def _intersect(l1, l2):
    (p, d), (q, e) = l1, l2
    m = np.array([[d[0], -e[0]], [d[1], -e[1]]])
    t = np.linalg.solve(m, q - p)
    return p + t[0] * d


HIGHPASS_RADIUS = 2
# The high-pass and the speck filter smear activity outwards past the picture edge by about this
# many pixels on a 320-pixel-wide map (measured: the raw outline includes the bezel).
EDGE_INSET_PER_320 = 2.0


class Activity:
    """Accumulates frame-to-frame change of fine detail."""

    def __init__(self):
        self.prev = self.total = None
        self.count = 0

    def add(self, luma):
        y = np.asarray(luma, dtype=np.float32)
        hp = y - _box(y, HIGHPASS_RADIUS)
        if self.prev is not None and self.prev.shape == hp.shape:
            d = np.abs(hp - self.prev)
            self.total = d if self.total is None else self.total + d
            self.count += 1
        self.prev = hp

    def take(self):
        """Mean activity map since the last take(), or None if no frames were added."""
        out = None if not self.count else self.total / self.count
        self.total, self.count = None, 0
        return out


def find_screen(activity):
    """Returns (corners, info). corners is a (4, 2) array TL, TR, BR, BL in pixels of the map.

    Raises NoScreen when the map is unusable (too little motion, no clear outline, ...).
    """
    h, w = activity.shape
    # Background sits near the lower quartile, the picture near the top decile; cut a quarter of the
    # way up. (Otsu cut too high: dim or quiet parts of the picture fell out of the mask.)
    lo, hi = np.percentile(activity, [25, 90])
    thr = lo + 0.25 * (hi - lo)
    mask = _box(activity > thr, 1) > 0.5          # majority filter: drops specks, fills pinholes
    blob = _blob(mask)
    frac = blob.mean()
    if not 0.04 < frac < 0.9:
        raise NoScreen("active area is %.0f%% of the frame" % (100 * frac))
    inside, outside = activity[blob].mean(), activity[~blob].mean()
    if inside < 2.0 or inside < 2.5 * outside:
        raise NoScreen("too little motion on screen (%.1f inside, %.1f outside)" % (inside, outside))

    solid = _fill_holes(blob)                      # outer outline only
    edge = solid.copy()                            # outline pixels: in the region with a neighbour outside
    edge[1:-1, 1:-1] &= ~(solid[:-2, 1:-1] & solid[2:, 1:-1] & solid[1:-1, :-2] & solid[1:-1, 2:])
    ys, xs = np.nonzero(edge)
    pts = np.stack([xs, ys], axis=1).astype(float)
    # Keep only lines with the whole blob on one side: holes and notches inside the picture also
    # produce straight outline runs, but those cut through the blob.
    centre = pts.mean(axis=0)
    by, bx = np.nonzero(solid)
    body = np.stack([bx, by], axis=1)[::7].astype(float)
    rough = []
    for theta, rho, votes in _hough_lines(pts, activity.shape, count=8):
        n = np.array([np.cos(theta), np.sin(theta)])
        side = (body @ n - rho) * np.sign(centre @ n - rho)
        if votes >= 15 and np.mean(side > -2.5) > 0.97:
            rough.append((theta, rho, votes))
    if len(rough) < 4:
        raise NoScreen("outline does not have four straight edges")
    rough = rough[:4]
    # Put the four edges in order around the blob, then the corners are where neighbours cross.
    sides = []
    for theta, rho, _ in rough:
        n = np.array([np.cos(theta), np.sin(theta)])
        foot = centre - (centre @ n - rho) * n     # point of the line nearest the centre
        sides.append((np.arctan2(foot[1] - centre[1], foot[0] - centre[0]), (foot, np.array([-n[1], n[0]]))))
    sides = [line for _, line in sorted(sides, key=lambda s: s[0])]
    try:
        quad = np.array([_intersect(sides[i - 1], sides[i]) for i in range(4)])
    except np.linalg.LinAlgError:
        raise NoScreen("outline edges are parallel")

    lines, residuals = [], []
    for i in range(4):
        a, b = quad[i], quad[(i + 1) % 4]
        line = ((a + b) / 2, (b - a) / np.linalg.norm(b - a))
        length = np.linalg.norm(b - a)
        for tol in (3.0, 2.0, 1.5):                # least-squares polish of the voted line
            c, d = line
            rel = pts - c
            along = rel @ d
            dist = np.abs(rel @ np.array([-d[1], d[0]]))
            sel = (dist < tol) & (np.abs(along) < 0.42 * length)   # stay clear of the rounded corners
            if sel.sum() < 8:
                raise NoScreen("an edge of the outline is not straight enough")
            line = _fit_line(pts[sel])
        if sel.sum() < 0.15 * length:
            raise NoScreen("an edge of the outline is not straight enough")
        residuals.append(float(np.sqrt(np.mean(dist[sel] ** 2))))
        c, d = line                                # move the edge inwards to undo the filter smear
        normal = np.array([-d[1], d[0]])
        centre = quad.mean(axis=0)
        if (centre - c) @ normal < 0:
            normal = -normal
        lines.append((c + normal * EDGE_INSET_PER_320 * w / 320.0, d))
    corners = np.array([_intersect(lines[i - 1], lines[i]) for i in range(4)])

    # Order: around the centre, starting so that the first edge is the top one (camera roughly upright).
    centre = corners.mean(axis=0)
    ang = np.arctan2(corners[:, 1] - centre[1], corners[:, 0] - centre[0])
    corners = corners[np.argsort(ang)]             # clockwise on screen, since y points down
    mids = [(corners[i] + corners[(i + 1) % 4])[1] for i in range(4)]
    corners = np.roll(corners, -int(np.argmin(mids)), axis=0)

    cross = []
    for i in range(4):
        u, v = corners[(i + 1) % 4] - corners[i], corners[(i + 2) % 4] - corners[(i + 1) % 4]
        cross.append(u[0] * v[1] - u[1] * v[0])
    if min(cross) <= 0:
        raise NoScreen("outline is not convex")
    margin = 0.15
    if (corners[:, 0].min() < -margin * w or corners[:, 0].max() > (1 + margin) * w
            or corners[:, 1].min() < -margin * h or corners[:, 1].max() > (1 + margin) * h):
        raise NoScreen("screen extends too far outside the camera frame")
    info = {"area": round(float(frac), 3), "inside": round(float(inside), 2),
            "outside": round(float(outside), 2), "edge_rms": round(max(residuals), 2)}
    return corners, info


def homography(src, dst):
    """3x3 matrix taking the four src points onto the four dst points."""
    rows, rhs = [], []
    for (x, y), (u, v) in zip(src, dst):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        rhs += [u, v]
    return np.append(np.linalg.solve(np.array(rows, dtype=float), np.array(rhs, dtype=float)), 1.0).reshape(3, 3)


def apply_homography(m, pts):
    p = np.c_[np.asarray(pts, dtype=float), np.ones(len(pts))] @ m.T
    return p[:, :2] / p[:, 2:]


UNIT_SQUARE = [(0, 0), (1, 0), (1, 1), (0, 1)]


class ScreenTracker:
    """Keeps the screen corners current while frames flow in.

    Corners are TL, TR, BR, BL as fractions of the camera frame. Every `window` frames the outline
    is measured; one measurement jitters by a pixel or two, so the calibration only changes when
    several agree:
      - "moved":  `confirm` consecutive measurements away from the current corners but close to
                  each other. Black bars (a letterboxed film) look like a smaller screen and are
                  ignored: the picture, not the panel, shrank.
      - "refined": the median of `refine` measurements near the current corners differs a little.
    add() returns a dict describing the change when one happens, else None.
    """

    def __init__(self, corners, window=300, deadband=0.02, confirm=3, refine=8, refine_step=0.006):
        self.corners = np.array(corners, dtype=float)
        self.window, self.deadband, self.confirm = window, deadband, confirm
        self.refine, self.refine_step = refine, refine_step
        self.activity = Activity()
        self.frames = 0
        self.near, self.away = [], []
        self.last_reject = None
        self.status = None        # one line about the latest measurement, set once per window

    @staticmethod
    def distance(a, b):
        """Largest corner displacement, as a fraction of frame width (assumes a 16:9 camera frame)."""
        d = np.asarray(a) - np.asarray(b)
        return float(np.hypot(d[:, 0], d[:, 1] * 9 / 16).max())

    def is_letterbox(self, cand):
        """True when cand is the current screen with bars on two opposite sides."""
        r = apply_homography(homography(self.corners, UNIT_SQUARE), cand)   # cand in screen coordinates
        tol = 0.035
        left, right = (r[0, 0] + r[3, 0]) / 2, (r[1, 0] + r[2, 0]) / 2
        top, bottom = (r[0, 1] + r[1, 1]) / 2, (r[3, 1] + r[2, 1]) / 2
        skew = max(abs(r[0, 0] - r[3, 0]), abs(r[1, 0] - r[2, 0]), abs(r[0, 1] - r[1, 1]), abs(r[3, 1] - r[2, 1]))
        if skew > tol:
            return False
        x_same = abs(left) < tol and abs(right - 1) < tol
        y_same = abs(top) < tol and abs(bottom - 1) < tol
        x_bars = left > tol and right < 1 - tol
        y_bars = top > tol and bottom < 1 - tol
        return (x_same and y_bars) or (y_same and x_bars)

    def add(self, luma):
        self.activity.add(luma)
        self.frames += 1
        if self.frames < self.window:
            return None
        self.frames = 0
        amap = self.activity.take()
        try:
            px, info = find_screen(amap)
        except NoScreen as e:
            self.last_reject = str(e)
            self.status = "no measurement: " + self.last_reject
            return None
        self.last_reject = None
        h, w = amap.shape
        cand = px / [w, h]
        dist = self.distance(cand, self.corners)
        self.status = "measured %.4f from current corners" % dist

        if dist <= self.deadband:
            self.away = []
            self.near.append(cand)
            self.status += " (near %d/%d)" % (len(self.near), self.refine)
            if len(self.near) < self.refine:
                return None
            median = np.median(np.array(self.near), axis=0)
            self.near = []
            if self.distance(median, self.corners) < self.refine_step:
                return None
            return self._adopt(median, "refined", info)

        if self.is_letterbox(cand):
            self.last_reject = "black bars on screen"
            self.status += ", ignored: black bars on screen"
            return None
        if self.away and self.distance(cand, self.away[-1]) > 0.75 * self.deadband:
            self.away = []                           # still moving, or a one-off bad fit: start over
        self.away.append(cand)
        self.status += " (away %d/%d)" % (len(self.away), self.confirm)
        if len(self.away) < self.confirm:
            return None
        median = np.median(np.array(self.away), axis=0)
        return self._adopt(median, "moved", info)

    def _adopt(self, corners, kind, info):
        old = self.corners
        self.corners = corners
        self.near, self.away = [], []
        return {"change": kind, "corners": np.round(corners, 4).tolist(), "previous": np.round(old, 4).tolist(),
                "shift": round(self.distance(corners, old), 4), "fit": info}
