# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Live break detection from camera fingerprints.

Feed every fingerprint to Detector.push(t, zones) and channel changes, with that channel's
templates, to Detector.set_channel(name, templates, t); set_templates() swaps the set of the
current channel (library update). Templates are (entry, features, weights) as returned by
Library.for_channel(). The interface mirrors the Java core (android/src/adshut/core/Detector).
The detector keeps the last few seconds on a fixed 5 fps grid, matches the channel's templates
against the newest stretch and runs a two-state machine:

  not in break --mute marker (kind "start")--> in break --unmute marker (kind "end")--> not in break

Markers are learned from the user's own presses (learn.py), so what counts as the end of a break
(e.g. whether the station's trailers stay muted) follows the user's habit. The detector fails
open (back to sound) when a break it muted runs too long, the channel changes or the picture
stops. A break the user started by muting can be ended by an unmute marker, but never by those
safety rules: the user may have muted for a reason of their own.

Each call returns a list of actions, dicts with "action" in
  "mute" / "unmute"   what to do with the sound, with "reason" and the "template" responsible
  "hit"               a template matched (for logging and learning)
"""
from collections import deque

import numpy as np

from adshut import fp
from adshut import match

THRESHOLD = 0.8            # genuine repeats score 0.9-1.0 through the camera, non-matches < 0.65
COOLDOWN_S = 15            # one occurrence of a template fires once
MAX_BREAK_S = 15 * 60      # longest break we believe in (Hungarian commercial TV runs 10+ min breaks)
SETTLE_S = 10              # unmute markers are ignored this soon after a break started
GAP_S = 10                 # picture missing this long ends a break (fail open)
BUFFER_S = 12


class Detector:
    def __init__(self, threshold=THRESHOLD):
        self.threshold = threshold
        self.channel = None
        self.templates = []
        self.groups = {}
        self.in_break = False
        self.break_since = None
        self.started_by = None     # "detector" or "user"
        self.last_fired = {}
        self.slots = deque(maxlen=int(BUFFER_S * fp.RATE))   # (feat, weight, valid)
        self.grid_t0 = None
        self.last_slot = None
        self.last_t = None

    # ------------------------------------------------------------ inputs

    def set_channel(self, name, templates, t):
        actions = []
        if name != self.channel:
            if self.in_break:
                actions += self._fail_open(t, "channel changed")
            self.channel = name
            self._load(templates)
            self.slots.clear()
            self.grid_t0 = None
        return actions

    def set_templates(self, templates):
        self._load(templates)

    def _load(self, templates):
        """Groups templates by length so each group is scored in one batched operation."""
        self.templates = templates
        self.groups = {}
        for entry, tf, tw in templates:
            self.groups.setdefault(len(tf), ([], [], [], []))
            g = self.groups[len(tf)]
            g[0].append(entry)
            g[1].append(tf)
            g[2].append(tw / max(float(tw.sum()), 1e-6) * len(tw))
            g[3].append(tw)
        self.groups = {m: (e, np.stack(f), np.stack(w), np.stack(r)) for m, (e, f, w, r) in self.groups.items()}

    def user_override(self, muted, t):
        """The user muted or unmuted by hand: follow them until the next marker."""
        if not muted and self.in_break:
            self._reset()
        elif muted and not self.in_break:
            self.in_break, self.break_since, self.started_by = True, t, "user"

    def tick(self, t):
        """Call regularly even without frames, so a stalled picture still ends a break."""
        if self.in_break and self.last_t is not None and t - self.last_t > GAP_S:
            return self._fail_open(t, "picture lost")
        if self.in_break and t - self.break_since > MAX_BREAK_S:
            return self._fail_open(t, "break too long")
        return []

    def push(self, t, zones):
        actions = self.tick(t)
        feat, weight = fp.features(zones[None])
        self._grid_put(t, feat[0], weight[0])
        self.last_t = t
        if not self.templates:
            return actions
        slots = list(self.slots)
        for m, (entries, tf, tw, raw) in self.groups.items():
            if len(slots) < m + 1:
                continue
            lf = np.stack([s[0] for s in slots[-(m + 1):]])            # (m+1, d)
            corr = np.einsum("kid,jd->kij", tf, lf)                     # (k, m, m+1)
            corr = match._max_filter(corr, match.SLACK)
            idx = np.arange(m)
            k = max(1, int(round(match.KEEP * m)))
            # Template ending at the newest slot (offset 1) or the one before (offset 0); each scored
            # on its best KEEP fraction of frames (match.robust_scores), featureless frames excluded.
            sc = []
            for o in (0, 1):
                d = np.where(raw > match.WEIGHT_MIN, corr[:, idx, idx + o], -1.0)
                sc.append((-np.partition(-d, k - 1, axis=1)[:, :k]).mean(axis=1))
            sc = np.maximum(sc[0], sc[1])
            for entry, score in zip(entries, sc):
                if score < entry.get("threshold", self.threshold):
                    continue
                name = entry["name"]
                if t - self.last_fired.get(name, -1e9) < COOLDOWN_S:
                    continue
                self.last_fired[name] = t
                actions.append({"action": "hit", "t": t, "template": name, "kind": entry["kind"],
                                "score": round(float(score), 3)})
                actions += self._on_hit(entry["kind"], name, t)
        return actions

    # ------------------------------------------------------------ state machine

    def _on_hit(self, kind, name, t):
        if kind == "start" and not self.in_break:
            self.in_break, self.break_since, self.started_by = True, t, "detector"
            return [{"action": "mute", "t": t, "reason": "mute marker", "template": name}]
        if kind == "end" and self.in_break and t - self.break_since > SETTLE_S:
            self._reset()
            return [{"action": "unmute", "t": t, "reason": "unmute marker", "template": name}]
        return []

    def _reset(self):
        self.in_break, self.break_since, self.started_by = False, None, None

    def _fail_open(self, t, reason):
        """Ends the break; unmutes only if the detector muted."""
        ours = self.started_by == "detector"
        self._reset()
        return [{"action": "unmute", "t": t, "reason": reason, "template": None}] if ours else []

    # ------------------------------------------------------------ 5 fps grid

    def _grid_put(self, t, feat, weight):
        if self.grid_t0 is None or (self.last_t is not None and t - self.last_t > 2.0):
            self.slots.clear()                              # gap: old frames no longer adjoin new ones
            self.grid_t0, self.last_slot = t, -1
        k = int(round((t - self.grid_t0) * fp.RATE))
        if k <= self.last_slot:                             # two frames in one slot: keep the newer
            if self.slots:
                self.slots[-1] = (feat, weight, True)
            return
        prev = self.slots[-1] if self.slots else None
        for slot in range(self.last_slot + 1, k):           # skipped slot: nearest frame fills it
            if prev is not None and (slot - self.last_slot) <= (k - slot):
                self.slots.append(prev)
            else:
                self.slots.append((feat, weight, True))
        self.slots.append((feat, weight, True))
        self.last_slot = k
