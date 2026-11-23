#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Contact sheet of recorder review images: sheet.py OUT.jpg HH:MM:SS SECONDS [STEP] [DATE]"""
import io, os, sys, time
from datetime import datetime
from PIL import Image, ImageDraw

from adshut.fp import DATA


def frames(day, t0, t1):
    out = []
    for hour in range(int(time.strftime("%H", time.localtime(t0))), int(time.strftime("%H", time.localtime(t1))) + 1):
        base = os.path.join(DATA, "thumbs", day, "%02d" % hour)
        if not os.path.exists(base + ".idx"):
            continue
        blob = open(base + ".mjpg", "rb")
        for line in open(base + ".idx"):
            t, off, ln = line.split()
            t = float(t)
            if t0 <= t <= t1:
                blob.seek(int(off))
                out.append((t, Image.open(io.BytesIO(blob.read(int(ln)))).convert("RGB")))
    return out


def main():
    out, hms, secs = sys.argv[1], sys.argv[2], float(sys.argv[3])
    step = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
    day = sys.argv[5] if len(sys.argv) > 5 else datetime.now().strftime("%Y-%m-%d")
    t0 = datetime.strptime(day + " " + hms, "%Y-%m-%d %H:%M:%S").timestamp()
    fr = frames(day, t0, t0 + secs)
    picked, last = [], -1e9
    for t, im in fr:
        if t - last >= step - 0.05:
            picked.append((t, im))
            last = t
    cols = 6
    w, h = 240, 135
    rows = (len(picked) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * (h + 14)), (20, 20, 20))
    d = ImageDraw.Draw(sheet)
    for i, (t, im) in enumerate(picked):
        x, y = (i % cols) * w, (i // cols) * (h + 14)
        sheet.paste(im.resize((w, h)), (x, y + 14))
        d.text((x + 3, y + 1), time.strftime("%H:%M:%S", time.localtime(t)), fill=(255, 255, 0))
    sheet.save(out, quality=85)
    print(out, len(picked), "images")


if __name__ == "__main__":
    main()
