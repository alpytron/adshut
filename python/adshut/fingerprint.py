# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Camera frame (JPEG) -> rectified screen image -> 16x9 zone fingerprint.

The frame is decoded at reduced size straight to YCbCr, the screen area (corners as fractions of
the frame: TL, TR, BR, BL) is warped to rect_size with Pillow's PERSPECTIVE/BILINEAR transform,
and the zone means are rounded to bytes. The Java core reproduces this from raw camera YUV
(android/src/adshut/core/Fingerprinter).
"""
import io

import numpy as np
from PIL import Image

from adshut.screen import homography

RECT_SIZE = (320, 180)
GRID = (16, 9)


def decode(jpeg, rect_size=RECT_SIZE):
    """Camera frame as a small YCbCr image (decoded at reduced size, straight to YCbCr)."""
    img = Image.open(io.BytesIO(jpeg))
    img.draft("YCbCr", tuple(rect_size))
    return img if img.mode == "YCbCr" else img.convert("YCbCr")


def perspective_coeffs(corners, src_size, dst_size):
    """PIL PERSPECTIVE coefficients mapping the output rectangle onto the screen corners."""
    sw, sh = src_size
    dw, dh = dst_size
    src = [(x * sw, y * sh) for x, y in corners]
    return homography([(0, 0), (dw, 0), (dw, dh), (0, dh)], src).ravel()[:8].tolist()


class Fingerprinter:
    def __init__(self, corners, rect_size=RECT_SIZE, grid=GRID):
        rw, rh = rect_size
        gw, gh = grid
        if rw % gw or rh % gh:
            raise ValueError("rect_size must be a multiple of grid")
        self.rect_size, self.grid = tuple(rect_size), tuple(grid)
        self.corners = corners
        self.persp = self.persp_size = None

    def set_corners(self, corners):
        self.corners = corners
        self.persp = None

    def rectify(self, img):
        """Screen area of the camera frame as an image of rect_size."""
        if self.persp is None or img.size != self.persp_size:
            self.persp = perspective_coeffs(self.corners, img.size, self.rect_size)
            self.persp_size = img.size
        return img.transform(self.rect_size, Image.PERSPECTIVE, self.persp, Image.BILINEAR)

    def zones(self, rect):
        gw, gh = self.grid
        rw, rh = rect.size
        a = np.asarray(rect, dtype=np.float32).reshape(gh, rh // gh, gw, rw // gw, 3)
        return np.rint(a.mean(axis=(1, 3))).astype(np.uint8)
