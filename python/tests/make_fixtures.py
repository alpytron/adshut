#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Generates spec/fixtures from SYNTHETIC inputs with the Python reference implementation.

Everything here is drawn by this script from a fixed random seed: an invented broadcast (a
programme, an opening ident, commercials, a closing ident, the programme again), the templates
of those idents as a learner would have cut them from an earlier airing, and a camera view of an
invented room with a TV in it. No recorded broadcast or camera image is used, so the fixtures
carry no third-party content.

Both test suites (python/tests/test_parity.py, android/test/.../ParityTest.java) must reproduce
the expected outputs. Regenerate only when the reference behaviour changes on purpose:
    PYTHONPATH=python python3 python/tests/make_fixtures.py
"""
import io
import json
import os
import shutil

import numpy as np
from PIL import Image, ImageDraw

from adshut import fp
from adshut.detect import Detector
from adshut.fingerprint import Fingerprinter, decode
from adshut.screen import Activity, find_screen

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(ROOT, "spec", "fixtures")
CHANNEL = "Example Channel"
T0 = 1767268800.0               # 2026-01-01 12:00 UTC
RATE = 5.0
CAM_W, CAM_H = 640, 360
QUAD = [(170, 42), (505, 98), (522, 318), (128, 276)]     # the TV in the camera frame, TL TR BR BL


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


def broadcast(rng):
    """Zones of the synthetic stretch and the template zones cut from an 'earlier airing'."""
    pics = []
    def shots(seconds, shot_s):
        n = int(seconds * RATE)
        while n > 0:
            sc, m = scene(rng), min(n, int(shot_s * RATE))
            pics.extend(zones(render(sc, k)) for k in range(m))
            n -= m
    shots(40, 6)
    pics.extend(zones(ident(0, k)) for k in range(int(5 * RATE)))
    shots(90, 15)
    pics.extend(zones(ident(1, k)) for k in range(int(4 * RATE)))
    shots(40, 6)
    cam = np.random.default_rng(7)
    stream = np.zeros(len(pics), fp.REC)
    stream["t"] = T0 + np.arange(len(pics)) / RATE + rng.uniform(-0.03, 0.03, len(pics))
    stream["z"] = [through_camera(p, cam, 0.9, 8) for p in pics]
    earlier = np.random.default_rng(11)
    tpls = [("example-start-a", "start", 0.8, [ident(0, k) for k in range(25)]),
            ("example-start-b", "start", 0.85, [ident(0, k) for k in range(5, 20)]),
            ("example-end-a", "end", 0.85, [ident(1, k) for k in range(20)]),
            ("example-start-unaired", "start", 0.85, [ident(2, k) for k in range(20)])]
    tpls = [(n, kind, thr, np.array([through_camera(zones(i), earlier, 1.15, -10) for i in imgs]))
            for n, kind, thr, imgs in tpls]
    return stream, tpls


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


def main():
    rng = np.random.default_rng(2026)
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(os.path.join(OUT, "templates"))

    stream, tpls = broadcast(rng)
    stream.tofile(os.path.join(OUT, "stream.bin"))

    # 1. features of the first 200 frames of the stream
    f, w = fp.features(stream["z"][:200])
    f.astype("<f4").tofile(os.path.join(OUT, "features.f32"))
    w.astype("<f4").tofile(os.path.join(OUT, "weights.f32"))

    # 2. detector over the stream with a frozen template set
    tpl = []
    with open(os.path.join(OUT, "templates", "index.tsv"), "w", encoding="utf-8") as idx:
        for name, kind, thr, z in tpls:
            z.tofile(os.path.join(OUT, "templates", name + ".zones"))
            idx.write("%s\t%s\t%s\t%d\t%s\n" % (name, kind, thr, len(z), CHANNEL))
            tf, tw = fp.features(z)
            tpl.append(({"name": name, "kind": kind, "threshold": thr, "channel": CHANNEL}, tf, tw))
    det = Detector()
    acts = det.set_channel(CHANNEL, tpl, float(stream["t"][0]))
    for t, z in zip(stream["t"], stream["z"]):
        acts += det.push(float(t), z)
    with open(os.path.join(OUT, "actions.tsv"), "w") as out:
        for a in acts:
            out.write("%.3f\t%s\t%s\t%s\t%s\n" % (a["t"], a["action"], a.get("template") or "",
                                                 a.get("kind", ""), "%.4f" % a["score"] if "score" in a else ""))
    assert [a["action"] for a in acts if a["action"] != "hit"] == ["mute", "unmute"], acts
    assert not any(a.get("template") == "example-start-unaired" for a in acts)

    # 3. screen finder: activity map arithmetic on 20 camera frames; corners from the map of 300
    cams = camera_frames(300, rng)
    frames = np.array([np.asarray(c.convert("L").resize((320, 180), Image.BILINEAR)) for c in cams])
    frames[:20].tofile(os.path.join(OUT, "luma_20x320x180.u8"))
    act = Activity()
    for fr in frames[:20]:
        act.add(fr)
    act.take().astype("<f4").tofile(os.path.join(OUT, "activity20.f32"))
    act = Activity()
    for fr in frames:
        act.add(fr)
    amap = act.take()
    amap.astype("<f4").tofile(os.path.join(OUT, "activity.f32"))
    corners, info = find_screen(amap)
    truth = np.array(QUAD) / 2.0
    assert np.abs(corners - truth).max() < 6, (corners, truth)
    json.dump({"corners_px": np.round(corners, 3).tolist(), "info": info}, open(os.path.join(OUT, "screen.json"), "w"), indent=1)

    # 4. fingerprint of a camera frame: JPEG path (Python) and YUV planes (Java), +-2 levels
    buf = io.BytesIO()
    cams[0].save(buf, "JPEG", quality=85)
    jpeg = buf.getvalue()
    open(os.path.join(OUT, "camera.jpg"), "wb").write(jpeg)
    corners_f = [[round(x / CAM_W, 4), round(y / CAM_H, 4)] for x, y in QUAD]
    fpr = Fingerprinter(corners_f)
    zones_ = fpr.zones(fpr.rectify(decode(jpeg)))
    zones_.tofile(os.path.join(OUT, "camera.zones"))
    full = np.asarray(Image.open(io.BytesIO(jpeg)).convert("YCbCr")).astype(np.float32)
    H, W = full.shape[:2]
    full[..., 0].astype(np.uint8).tofile(os.path.join(OUT, "camera_y_%dx%d.u8" % (W, H)))
    for k, n in ((1, "u"), (2, "v")):
        np.rint(full[..., k].reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3))).astype(np.uint8).tofile(
            os.path.join(OUT, "camera_%s_%dx%d.u8" % (n, W // 2, H // 2)))
    json.dump({"corners": corners_f, "width": W, "height": H}, open(os.path.join(OUT, "camera.json"), "w"))
    print("fixtures: %d frames, %d templates, %d actions, corners %s (truth %s)"
          % (len(stream), len(tpl), len(acts), np.round(corners, 1).tolist(), truth.tolist()))
    for a in acts:
        print("  %+.1f s  %s %s %s" % (a["t"] - T0, a["action"], a.get("template") or "", a.get("score", "")))


if __name__ == "__main__":
    main()
