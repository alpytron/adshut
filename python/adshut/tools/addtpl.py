#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Cuts a template out of the recording.

    addtpl.py CHANNEL KIND HH:MM:SS.s SECONDS [--day YYYY-MM-DD] [--note TEXT]

KIND is start, end or boundary (see library.py).
"""
import argparse
import os
from datetime import datetime

from adshut import fp
from adshut.library import KINDS, Library


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("channel")
    ap.add_argument("kind", choices=KINDS)
    ap.add_argument("start", help="HH:MM:SS or HH:MM:SS.s")
    ap.add_argument("seconds", type=float)
    ap.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--note", default="")
    ap.add_argument("--data", default=fp.DATA, help="data directory (default: $ADSHUT_DATA or ~/.local/share/adshut)")
    ap.add_argument("--threshold", type=float, help="match threshold for this template (default 0.8)")
    args = ap.parse_args()
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in args.start else "%Y-%m-%d %H:%M:%S"
    t0 = datetime.strptime(args.day + " " + args.start, fmt).timestamp()
    tl = fp.Timeline(fp.load_records(args.day, os.path.expanduser(args.data)))
    s, m = tl.index(t0), int(round(args.seconds * fp.RATE))
    if s < 0 or s + m > len(tl) or not tl.valid[s:s + m].all():
        raise SystemExit("no complete recording for that stretch")
    name = Library(os.path.expanduser(args.data)).add(args.channel, args.kind, tl.zones[s:s + m], tl.time(s), args.note, threshold=args.threshold)
    print("added", name, "(%d frames)" % m)


if __name__ == "__main__":
    main()
