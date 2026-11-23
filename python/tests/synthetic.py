# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Synthetic test material: an invented broadcast (shots, channel idents) and a camera view of an
invented room with a TV in it. Used by make_fixtures.py and e2e_command_host.py, so the tests
need no recorded broadcast or camera images.
"""
import io

import numpy as np
from PIL import Image, ImageDraw

RATE = 5.0
CAM_W, CAM_H = 640, 360
QUAD = [(170, 42), (505, 98), (522, 318), (128, 276)]     # the TV in the camera frame, TL TR BR BL
CORNERS = [[round(x / CAM_W, 4), round(y / CAM_H, 4)] for x, y in QUAD]


# ---- the broadcast: pictures as 90x160 YCbCr, zones as 9x16 block means --------------------

def scene(rng):
    """A random 'shot': smooth colour field parameters plus a few moving blobs."""
    return {"f": rng.uniform(0.5, 3.0, (3, 2)), "p": rng.uniform(0, 6.3, (3, 2)),
            "base": rng.uniform([40, 100, 100], [200, 156, 156]), "amp": rng.uniform([20, 5, 5], [70, 25, 25]),
            "blobs": rng.uniform(0, 1, (4, 4)), "v": rng.uniform(-0.02, 0.02, (4, 2))}


def render(sc, k, h=90, w=160):
    """Frame k of shot sc as float YCbCr (h, w, 3)."""
    y, x = np.mgrid[0:h, 0:w] / np.array([h, w])[:, None, None]
    img = np.empty((h, w, 3))
    for c in range(3):
        img[..., c] = sc["base"][c] + sc["amp"][c] * np.sin(6.28 * (sc["f"][c, 0] * x + sc["f"][c, 1] * y) + sc["p"][c, 0] + 0.03 * k)
    for (bx, by, r, s), (vx, vy) in zip(sc["blobs"], sc["v"]):
        cx, cy = (bx + vx * k) % 1, (by + vy * k) % 1
        m = ((x - cx) ** 2 + ((y - cy) * 0.56) ** 2) < (0.04 + 0.08 * r) ** 2
        img[m, 0] = 30 + 200 * s
    return img


def ident(kind, k, h=90, w=160):
    """A channel ident animation (kind 0 = opening, 1 = closing, 2 = one that never airs)."""
    y, x = np.mgrid[0:h, 0:w] / np.array([h, w])[:, None, None]
    a = 0.12 * k + kind * 1.3
    u = (x - 0.5) * np.cos(a) + (y - 0.5) * np.sin(a)
    img = np.empty((h, w, 3))
    img[..., 0] = 128 + 100 * np.sign(np.sin(12 * u + kind))
    img[..., 1] = 128 + 40 * np.cos(3 * u + kind * 2)
    img[..., 2] = 128 - 40 * np.sin(5 * y + 0.05 * k + kind)
    r = 0.1 + 0.015 * k
    img[(x - 0.5) ** 2 + ((y - 0.5) * 0.56) ** 2 < r * r * (kind + 1) / 2, 0] = 235 - 60 * kind
    return img


def zones(img):
    return img.reshape(9, 10, 16, 10, 3).mean(axis=(1, 3))


def through_camera(z, rng, gain, offset):
    """What a camera adds: exposure (gain/offset on luma), a white-balance shift, noise."""
    z = z.copy()
    z[..., 0] = z[..., 0] * gain + offset
    z[..., 1:] += rng.uniform(-6, 6, 2)
    return np.clip(np.rint(z + rng.normal(0, 1.5, z.shape)), 0, 255).astype(np.uint8)


# ---- the camera view of a room with the TV ------------------------------------------------

def homography(src, dst):
    """Coefficients for PIL's PERSPECTIVE transform mapping output points dst to input points src."""
    a, b = [], []
    for (x, y), (u, v) in zip(dst, src):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    return np.linalg.solve(np.array(a, float), np.array(b, float)).tolist()


def room(rng):
    img = Image.new("RGB", (CAM_W, CAM_H))
    px = np.zeros((CAM_H, CAM_W, 3))
    px[:] = np.linspace(150, 110, CAM_H)[:, None, None] * np.array([1.0, 0.95, 0.85])
    px += rng.normal(0, 4, px.shape)
    img = Image.fromarray(np.clip(px, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 300, CAM_W, CAM_H], fill=(92, 70, 52))                     # sideboard
    d.rectangle([20, 60, 95, 230], fill=(60, 90, 70))                          # plant
    d.rectangle([560, 30, 630, 140], outline=(40, 40, 40), width=4, fill=(200, 190, 170))   # picture frame
    d.polygon([(165, 37), (511, 93), (528, 323), (122, 281)], fill=(18, 18, 20))            # TV bezel
    return img


def camera_frames(n, rng):
    """n camera frames (PIL RGB) of the room with random shots on the TV, a new shot every 12 frames."""
    bg = room(rng)
    coeffs = homography([(0, 0), (320, 0), (320, 180), (0, 180)], QUAD)
    mask = Image.new("L", (CAM_W, CAM_H))
    ImageDraw.Draw(mask).polygon(QUAD, fill=255)
    sc = scene(rng)
    out = []
    for k in range(n):
        if k % 12 == 0:
            sc = scene(rng)
        ycc = np.clip(render(sc, k, 180, 320), 0, 255).astype(np.uint8)
        tv = Image.fromarray(ycc, "YCbCr").convert("RGB")
        # fine detail on the picture (text, edges) so the high-pass activity sees the screen
        dd = ImageDraw.Draw(tv)
        for _ in range(12):
            x, y = rng.integers(0, 300), rng.integers(0, 170)
            dd.rectangle([x, y, x + rng.integers(4, 20), y + rng.integers(2, 8)], fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
        warped = tv.transform((CAM_W, CAM_H), Image.PERSPECTIVE, coeffs, Image.BILINEAR)
        frame = bg.copy()
        frame.paste(warped, (0, 0), mask)
        arr = np.asarray(frame).astype(np.float32) + rng.normal(0, 2, (CAM_H, CAM_W, 3))
        out.append(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)))
    return out




class Camera:
    """Films given TV pictures (float YCbCr 180x320) in the synthetic room, as JPEG."""

    def __init__(self, rng):
        self.rng = rng
        self.bg = room(rng)
        self.coeffs = homography([(0, 0), (320, 0), (320, 180), (0, 180)], QUAD)
        self.mask = Image.new("L", (CAM_W, CAM_H))
        ImageDraw.Draw(self.mask).polygon(QUAD, fill=255)

    def jpeg(self, ycc, quality=85):
        tv = Image.fromarray(np.clip(ycc, 0, 255).astype(np.uint8), "YCbCr").convert("RGB")
        warped = tv.transform((CAM_W, CAM_H), Image.PERSPECTIVE, self.coeffs, Image.BILINEAR)
        frame = self.bg.copy()
        frame.paste(warped, (0, 0), self.mask)
        arr = np.asarray(frame).astype(np.float32) + self.rng.normal(0, 2, (CAM_H, CAM_W, 3))
        buf = io.BytesIO()
        Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=quality)
        return buf.getvalue()
