#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""End-to-end test of a host as on a Raspberry Pi with a non-smart TV, without hardware:

  frames   'command' source: tests/mjpeg_out.py plays a synthetic camera loop (tests/synthetic.py:
           an invented room and TV; 12 s programme, a 5 s channel ident, 13 s of ads) at 10 fps
  TV       'command' adapter: the toggle and toast commands append to ir.log (an IR blaster)
  remote   a named pipe stands in for the IR receiver's input device (KEY_MUTE events)
  server   a local template server with a token and one template for channel "tv": the ident as
           fingerprinted from an earlier, differently exposed airing

Checks: the template set syncs down, the detector toggles mute (once, at 5 fps from a 10 fps
source), a key press right after our own transmission is ignored as an echo, a later press is
taken as the user's unmute, and fingerprints and events are uploaded.

    PYTHONPATH=python python3 python/tests/e2e_command_host.py
Self-contained: everything it shows the host is drawn by tests/synthetic.py. Takes about 2 minutes.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
B = os.path.join(ROOT, "build", "pi")
PY = os.path.join(ROOT, "python")
ENV = dict(os.environ, PYTHONPATH=PY)
FPS = 10

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PY)
import numpy as np  # noqa: E402

from adshut.fingerprint import Fingerprinter, decode  # noqa: E402
from adshut.library import Library  # noqa: E402
from synthetic import CORNERS, Camera, ident, render, scene  # noqa: E402


def make_loop(path):
    """The camera loop the host watches: programme, opening ident, ads (30 s at FPS)."""
    rng = np.random.default_rng(3)
    cam = Camera(rng)
    with open(path, "wb") as out:
        for seconds, shot_s in ((12, 3), ("ident", 5), (13, 6.5)):
            if seconds == "ident":
                for k in range(shot_s * FPS):
                    out.write(cam.jpeg(ident(0, k * 5.0 / FPS, 180, 320)))
                continue
            for j in range(int(seconds / shot_s)):
                sc = scene(rng)
                for k in range(int(shot_s * FPS)):
                    out.write(cam.jpeg(render(sc, k * 5.0 / FPS, 180, 320)))


def make_template(data):
    """The opening ident as a learner would have cut it from an earlier airing (other camera noise
    and exposure), 5 s at the fingerprint rate."""
    cam = Camera(np.random.default_rng(4))
    fpr = Fingerprinter(CORNERS)
    zs = []
    for k in range(25):
        ycc = ident(0, k, 180, 320)
        ycc[..., 0] = ycc[..., 0] * 0.9 + 8
        zs.append(fpr.zones(fpr.rectify(decode(cam.jpeg(ycc)))))
    return Library(data).add("tv", "start", np.array(zs), time.time() - 86400, "synthetic e2e ident")


def run(*cmd, **kw):
    return subprocess.run(cmd, cwd=PY, env=ENV, capture_output=True, text=True, check=True, **kw).stdout.strip()


def main():
    shutil.rmtree(B, ignore_errors=True)
    os.makedirs(os.path.join(B, "server"))
    loop = os.path.join(B, "camera.mjpg")
    make_loop(loop)
    print("template", make_template(B + "/server"))
    token = run("python3", "-m", "adshut.server", "--data", B + "/server", "--add-tv", "pi-test")
    keys = os.path.join(B, "keys")
    os.mkfifo(keys)
    irlog = os.path.join(B, "ir.log")
    cfg = {
        "data_dir": B + "/host",
        "source": {"type": "command", "cmd": ["python3", PY + "/tests/mjpeg_out.py", loop, str(FPS)]},
        "tv": {"type": "command", "toggle": "echo toggle >> " + irlog, "toast": "echo 'toast {text}' >> " + irlog,
               "channel": "tv", "keys": {"device": keys, "mute": 113}},
        "server": {"url": "http://127.0.0.1:18766", "tv": "pi-test", "token": token},
        "corners": CORNERS, "auto_calibrate": False, "detector": "active", "toasts": True,
    }
    json.dump(cfg, open(B + "/host.json", "w"))
    srv = subprocess.Popen(["python3", "-m", "adshut.server", "--data", B + "/server", "--port", "18766",
                            "--bind", "127.0.0.1"], cwd=PY, env=ENV, stdout=open(B + "/server.log", "w"), stderr=subprocess.STDOUT)
    host = subprocess.Popen(["python3", "-m", "adshut.host", "--config", B + "/host.json"], cwd=PY, env=ENV,
                            stdout=open(B + "/host.log", "w"), stderr=subprocess.STDOUT)
    presses = []

    def remote():
        ev = struct.Struct("llHHi")
        with open(keys, "wb", buffering=0) as f:                 # opens once the host reads the pipe
            def press():
                t = time.time()
                f.write(ev.pack(int(t), 0, 1, 113, 1) + ev.pack(int(t), 0, 1, 113, 0))
                presses.append(t)
            for _ in range(450):
                if os.path.exists(irlog) and "toggle" in open(irlog).read():
                    break
                time.sleep(0.1)
            time.sleep(0.3)
            press()                       # echo of our own transmission: must be ignored
            time.sleep(6)
            press()                       # the user unmutes with the remote

    th = threading.Thread(target=remote, daemon=True)
    th.start()
    try:
        th.join(60)
        time.sleep(75)                    # let the sync run once more and upload the latest data
    finally:
        host.terminate()
        srv.terminate()
        host.wait(10)
        srv.wait(10)

    ok = True

    def check(cond, what):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + what)

    ir = open(irlog).read().split("\n") if os.path.exists(irlog) else []
    events = [json.loads(l) for l in open(B + "/host/events.jsonl")]
    mutes = [e for e in events if e["type"] == "mute" and not e.get("first")]
    lib = json.load(open(B + "/host/templates/index.json")) if os.path.exists(B + "/host/templates/index.json") else []
    check(any(e.get("origin") == "server" and e["channel"] == "tv" for e in lib), "template set synced from the server")
    decided = [e for e in events if e["type"] == "detector" and e.get("action") in ("mute", "unmute")]
    check(len(decided) >= 1 and ir.count("toggle") == len(decided),
          "one toggle per detector decision (%d toggles, %d decisions; the recording loops every 30 s)"
          % (ir.count("toggle"), len(decided)))
    check(any(l.startswith("toast \U0001F507") for l in ir), "toast command got the mute note")
    check(len(mutes) >= 1 and mutes[0]["muted"] and mutes[0].get("by_detector"), "mute logged as the detector's")
    check(len(presses) == 2, "remote sent two presses")
    users = [e for e in mutes if not e.get("by_detector")]
    check(len(users) == 1 and users[0]["muted"] is False and users[0]["t"] - presses[0] > 5,
          "echo ignored, second press logged as the user's unmute")
    orphans = subprocess.run(["pgrep", "-f", "tests/mjpeg_out[.]py"], capture_output=True, text=True).stdout.split()
    check(not orphans, "frame program stopped together with the host")
    fp_local = sum(os.path.getsize(os.path.join(B, "host", "fp", f)) for f in os.listdir(B + "/host/fp") if f.endswith(".bin"))
    tvdir = os.path.join(B, "server", "tvs", "pi-test")
    fp_srv = sum(os.path.getsize(os.path.join(tvdir, "fp", f)) for f in os.listdir(tvdir + "/fp")) if os.path.isdir(tvdir + "/fp") else 0
    n = fp_local // 440
    t = struct.unpack("<d", open(os.path.join(B, "host", "fp", sorted(os.listdir(B + "/host/fp"))[0]), "rb").read(8))[0]
    check(fp_srv > 0 and fp_local - fp_srv < 440 * 5 * 70, "fingerprints uploaded (%d of %d bytes)" % (fp_srv, fp_local))
    check(os.path.exists(tvdir + "/events.jsonl"), "events uploaded")
    print("frames: %d" % n)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
