# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Per-channel library of reference sequences (templates).

Each template is a few seconds of frame features cut from the recording, with a kind:

  start     a mute marker: picture that only airs at the start of or inside a break
  end       an unmute marker: picture that shows the programme is (about to be) back
  boundary  (legacy, ignored by the detector)

Most templates are learned from the user's presses (learn.py, origin "learned"); entries carry
optional "threshold", and "successes"/"strikes" counted from how the user reacted to them.
Stored under data/templates/: index.json (metadata) and <name>.npz (features, weights, zones).
Channels are keyed by name, not by the TV's channel id, which can change when the TV rescans.
Entries learned from a TV's data carry "client" (the TV id, "_host" for the server's own host).

review.json holds an admin's decisions from the template review page ({name: {"enabled", "by",
"t", "note"}}). They win over the learner's: they are applied whenever the index is loaded or
saved, so a learner run that started before the decision cannot undo it.
"""
import json
import os
import re
import time

import numpy as np

from adshut import fp

KINDS = ("start", "end", "boundary")
REVIEW = "review.json"


def load_review(tpl_dir):
    try:
        with open(os.path.join(tpl_dir, REVIEW)) as f:
            r = json.load(f)
        return r if isinstance(r, dict) else {}
    except (OSError, ValueError):
        return {}


def apply_review(entries, tpl_dir):
    """Applies the admin's enable/disable decisions to index entries (in place); returns them."""
    review = load_review(tpl_dir)
    for e in entries:
        r = review.get(e["name"])
        if r:
            e["review"] = r
            if r.get("enabled") is not None:
                e["enabled"] = bool(r["enabled"])
        else:
            e.pop("review", None)
    return entries


class Library:
    def __init__(self, data=fp.DATA, templates_dir=None):
        self.dir = templates_dir or os.path.join(data, "templates")
        os.makedirs(self.dir, exist_ok=True)
        self.index_file = os.path.join(self.dir, "index.json")
        self.reload()

    def reload(self):
        try:
            with open(self.index_file) as f:
                self.entries = json.load(f)
        except (OSError, ValueError):
            self.entries = []
        apply_review(self.entries, self.dir)
        self.loaded = {}
        self.mtime = os.path.getmtime(self.index_file) if os.path.exists(self.index_file) else 0

    def changed_on_disk(self):
        return os.path.exists(self.index_file) and os.path.getmtime(self.index_file) != self.mtime

    def save(self, force=False):
        """Writes the index (atomically). While `deferred` is set (a learner run in progress), only
        a forced save writes, so hosts and TVs never load a half-checked library."""
        if getattr(self, "deferred", False) and not force:
            return
        apply_review(self.entries, self.dir)
        tmp = self.index_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.entries, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.index_file)
        self.mtime = os.path.getmtime(self.index_file)

    def for_channel(self, channel):
        """Enabled templates of a channel as (entry, features, weights)."""
        out = []
        for e in self.entries:
            if e["channel"] == channel and e.get("enabled", True):
                if e["name"] not in self.loaded:
                    d = np.load(os.path.join(self.dir, e["name"] + ".npz"))
                    self.loaded[e["name"]] = (d["feat"], d["weight"])
                out.append((e,) + self.loaded[e["name"]])
        return out

    def add(self, channel, kind, zones, source_t, note="", origin="manual", threshold=None, extra=None):
        """Adds a template from (m, 9, 16, 3) zone fingerprints sampled at fp.RATE; returns its name."""
        assert kind in KINDS
        feat, weight = fp.features(zones)
        slug = re.sub(r"[^a-z0-9]+", "-", channel.lower()).strip("-")
        base = "%s-%s-%s" % (slug, kind, time.strftime("%Y%m%d-%H%M%S", time.localtime(source_t)))
        name, n = base, 1
        while any(e["name"] == name for e in self.entries):   # never overwrite another template
            n += 1
            name = "%s-%d" % (base, n)
        np.savez_compressed(os.path.join(self.dir, name + ".npz"), feat=feat, weight=weight, zones=zones)
        self.entries.append({
            "name": name, "channel": channel, "kind": kind, "frames": len(zones),
            "seconds": round(len(zones) / fp.RATE, 1), "source": round(source_t, 2),
            "source_ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(source_t)),
            "origin": origin, "note": note, "enabled": True,
        })
        if threshold:
            self.entries[-1]["threshold"] = threshold
        if getattr(self, "client", None):
            self.entries[-1]["client"] = self.client
        self.entries[-1].update(extra or {})
        self.save()
        return name
