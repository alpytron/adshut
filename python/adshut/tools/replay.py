#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Runs the live detector over a recorded day and lines its decisions up with the user's mutes.

    replay.py [DAY] [--channel NAME]

The channel comes from the day's events; --channel names the channel for recordings made before
the recorder logged channel names.
"""
import argparse
import time
from datetime import datetime

from adshut import fp
from adshut.detect import Detector
from adshut.library import Library


TOGGLE_S = 5      # a mute and unmute this close together was an accidental double press


def user_actions(events):
    """The user's own sound changes as (t, muted), without accidental quick toggles."""
    acts = [(e["t"], e["muted"]) for e in events
            if e["type"] == "mute" and not e.get("first") and not e.get("by_detector")]
    keep = []
    for a in acts:
        if keep and a[0] - keep[-1][0] < TOGGLE_S and a[1] != keep[-1][1]:
            keep.pop()             # this press undid the previous one: drop both
        else:
            keep.append(a)
    return keep


def hms(t):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:10]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day", nargs="?", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--channel", default=None)
    args = ap.parse_args()
    recs = fp.load_records(args.day)
    day0 = datetime.strptime(args.day, "%Y-%m-%d").timestamp()
    events = [e for e in fp.load_events() if day0 <= e["t"] < day0 + 86400]

    # Channel timeline and the user's own sound changes.
    chan = [(e["t"], e["name"]) for e in events if e["type"] == "channel" and e.get("name")]
    user = user_actions(events)
    default = args.channel or (chan[0][1] if chan else None)

    lib = Library()
    det = Detector()
    out = []
    ci = 0
    current = None
    t0 = time.time()
    for t, z in zip(recs["t"], recs["z"]):
        name = default
        while ci < len(chan) and chan[ci][0] <= t:
            ci += 1
        if ci:
            name = chan[ci - 1][1]
        if name != current:
            out += det.set_channel(name, lib.for_channel(name) if name else [], t)
            current = name
        out += det.push(t, z)
    took = time.time() - t0

    rows = [(a["t"], "detector", a) for a in out] + [(t, "user", {"muted": m}) for t, m in user]
    for t, who, a in sorted(rows, key=lambda r: r[0]):
        if who == "user":
            print("%s  USER      %s" % (hms(t), "mute" if a["muted"] else "unmute"))
        elif a["action"] == "hit":
            print("%s    hit     %-8s %.3f %s" % (hms(t), a["kind"], a["score"], a["template"]))
        else:
            print("%s  DETECTOR  %-6s  (%s)" % (hms(t), a["action"], a["reason"]))
    print("%d frames in %.1f s (%.2f ms per frame)" % (len(recs), took, 1000 * took / max(len(recs), 1)))


if __name__ == "__main__":
    main()
