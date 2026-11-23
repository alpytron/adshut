#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Exports enabled templates in the format the TV app reads (and the template server serves).

    export_templates.py OUTDIR [--channel NAME]

OUTDIR/index.tsv: one line per template: name, kind, threshold, frames, channel (tab-separated)
OUTDIR/<name>.zones: frames x 432 unsigned bytes (9x16 zones x Y,Cb,Cr), the raw fingerprints;
the app computes features itself, so the format does not depend on the feature definition.
"""
import argparse
import os

import numpy as np

from adshut.library import Library


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--channel")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    lib = Library()
    n = 0
    with open(os.path.join(args.outdir, "index.tsv"), "w") as idx:
        for e in lib.entries:
            if not e.get("enabled", True) or e["kind"] not in ("start", "end"):
                continue
            if args.channel and e["channel"] != args.channel:
                continue
            zones = np.load(os.path.join(lib.dir, e["name"] + ".npz"))["zones"].astype(np.uint8)
            zones.tofile(os.path.join(args.outdir, e["name"] + ".zones"))
            idx.write("%s\t%s\t%s\t%d\t%s\n" % (e["name"], e["kind"], e.get("threshold", 0.8), len(zones), e["channel"]))
            n += 1
    print("exported %d templates to %s" % (n, args.outdir))


if __name__ == "__main__":
    main()
