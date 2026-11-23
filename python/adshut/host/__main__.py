# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""adshut host: the live loop on a Linux machine (python3 -m adshut.host --config FILE).

Config (JSON); adapters are chosen per installation:
  data_dir      (default $ADSHUT_DATA or ~/.local/share/adshut) where events, fingerprints, review images and the template library live
  tv            {"type": "androidtv-app", "url": "http://<tv>:8765"}
                {"type": "command", "mute": ..., "unmute": ... | "toggle": ..., "toast": ..., "power": ...,
                 "channel": "tv", "keys": {"device": "/dev/input/...", "mute": 113}}   (see host/tv.py)
  source        {"type": "mjpeg", "url": "http://<tv>:8765/stream.mjpg"}
                {"type": "v4l2", "device": "/dev/video0", "size": "640x360", "fps": 10, "input_format": "mjpeg"}
                {"type": "command", "cmd": [...]}          (any program writing MJPEG to stdout)
  server        optional {"url": "http://<server>:8766", "tv": "<id>", "token": "..."}: upload data and
                download the template set (hosts that are not on the template server machine)
  detector      "off" | "shadow" | "active";  toasts: true | false
  thumb_interval_s  seconds between rectified review images of the TV picture, stored on this host
                only (never uploaded) for thumb_keep_days; default 0 = no review images
  corners, auto_calibrate, thumb_quality, thumb_keep_days
Older configs with tv_ip / cam_port mean the Android TV app at that address.
"""
import argparse
import json
import os
import sys
import threading
import urllib.request

from adshut.fingerprint import Fingerprinter, decode
from adshut.host.live import Breaks, Capture
from adshut.host.sources import CommandSource, MjpegSource, v4l2_command
from adshut.host.tv import AndroidTvApp, CommandTv
from adshut.sync import Sync
from adshut.library import Library
from adshut.store import Store
from adshut.fp import DATA

DEFAULTS = {
    "data_dir": DATA,
    "corners": [[0, 0], [1, 0], [1, 1], [0, 1]],
    "auto_calibrate": True,
    "detector": "shadow",
    "toasts": True,
    "thumb_interval_s": 0,           # review images of the TV picture, kept on this host only; 0 = none
    "thumb_quality": 60,
    "thumb_keep_days": 7,
}


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    if "tv" not in cfg and "tv_ip" in cfg:            # older configs: the Android TV app
        base = "http://%s:%d" % (cfg["tv_ip"], cfg.get("cam_port", 8765))
        cfg["tv"] = {"type": "androidtv-app", "url": base}
        cfg.setdefault("source", {"type": "mjpeg", "url": base + "/stream.mjpg", "status_url": base + "/status"})
    return cfg


def make_tv(cfg, store):
    t = cfg["tv"]
    if t["type"] == "androidtv-app":
        return AndroidTvApp(t["url"], store, t.get("live_app", "com.mediatek.wwtv.tvcenter"))
    if t["type"] == "command":
        return CommandTv(t, store)
    sys.exit("unknown tv type %r" % t["type"])


def make_source(cfg):
    s = cfg["source"]
    if s["type"] == "mjpeg":
        return MjpegSource(s["url"], s.get("status_url"))
    if s["type"] == "command":
        return CommandSource(s["cmd"])
    if s["type"] == "v4l2":
        return CommandSource(v4l2_command(s.get("device", "/dev/video0"), s.get("size", "640x360"),
                                          s.get("fps", 10), s.get("input_format", "mjpeg")))
    sys.exit("unknown source type %r" % s["type"])


def snapshot(cfg, out):
    """Saves one raw camera frame and its rectified version (corners from the config)."""
    url = cfg["source"]["url"].replace("stream.mjpg", "shot.jpg")
    jpeg = urllib.request.urlopen(url, timeout=15).read()
    with open(out + "-raw.jpg", "wb") as f:
        f.write(jpeg)
    fpr = Fingerprinter(cfg["corners"])
    fpr.rectify(decode(jpeg)).convert("RGB").save(out + "-rect.jpg", quality=90)
    print("wrote", out + "-raw.jpg", "and", out + "-rect.jpg")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--snapshot", metavar="PREFIX", help="save PREFIX-raw.jpg and PREFIX-rect.jpg, then exit")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.snapshot:
        snapshot(cfg, args.snapshot)
        return
    store = Store(cfg["data_dir"], thumb_keep_days=cfg["thumb_keep_days"])
    if cfg.get("server"):
        sv = cfg["server"]
        Sync(sv["url"], sv["tv"], sv["token"], cfg["data_dir"]).start()
    tv = make_tv(cfg, store)
    store.event("recorder", started=True)
    breaks = None
    if cfg["detector"] != "off":
        breaks = Breaks(tv, store, Library(store.dir), cfg["detector"], cfg["toasts"])
    cap = Capture(make_source(cfg), tv, store, breaks, cfg["corners"], cfg["auto_calibrate"],
                  cfg["thumb_interval_s"], cfg["thumb_quality"])
    threading.Thread(target=cap.run, daemon=True).start()
    tv.run()


if __name__ == "__main__":
    main()
