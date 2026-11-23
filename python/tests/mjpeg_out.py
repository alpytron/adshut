#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Plays a file of concatenated JPEG frames as a raw MJPEG stream on stdout, looping, at FPS:
a stand-in for ffmpeg/rpicam-vid when testing the command frame source.

    mjpeg_out.py FRAMES.mjpg [FPS]
"""
import sys
import time

data = open(sys.argv[1], "rb").read()
fps = float(sys.argv[2]) if len(sys.argv) > 2 else 10
starts = [i for i in range(len(data) - 1) if data[i] == 0xFF and data[i + 1] == 0xD8]
frames = [data[a:b] for a, b in zip(starts, starts[1:] + [len(data)])]
out = sys.stdout.buffer
i = 0
while True:
    out.write(frames[i % len(frames)])
    out.flush()
    i += 1
    time.sleep(1.0 / fps)
