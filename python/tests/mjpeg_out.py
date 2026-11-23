#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Writes recorded frames (u8 luma, W x H) as a raw MJPEG stream to stdout, looping, at FPS:
a stand-in for ffmpeg/rpicam-vid when testing the command frame source.

    mjpeg_out.py FRAMES.u8 W H [FPS] [COUNT]
"""
import io
import sys
import time

import numpy as np
from PIL import Image

path, w, h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
fps = float(sys.argv[4]) if len(sys.argv) > 4 else 10
count = int(sys.argv[5]) if len(sys.argv) > 5 else 300
frames = np.fromfile(path, dtype=np.uint8, count=w * h * count).reshape(-1, h, w)
out = sys.stdout.buffer
i = 0
while True:
    buf = io.BytesIO()
    Image.fromarray(frames[i % len(frames)]).convert("RGB").save(buf, "JPEG", quality=85)
    out.write(buf.getvalue())
    out.flush()
    i += 1
    time.sleep(1.0 / fps)
