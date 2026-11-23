#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Stand-in for the adshut TV app, for testing hosts without a TV.

Serves the app's HTTP interface from recorded camera frames (u8 luma, W x H each, looped):
  /state  /events?since=N&wait=S  /stream.mjpg  /shot.jpg  /status  /mute?on=  /toast?text=
and records what it was asked to do. Test controls:
  /_press?muted=1|0     the "user" presses mute on the remote
  /_channel?name=NAME   the "user" changes the channel
  /_log                 everything the host asked for (JSON)

    fake_tv_app.py FRAMES.u8 W H [--port 18765] [--fps 5] [--channel NAME]
"""
import argparse
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image


class App:
    def __init__(self, frames, fps, channel):
        self.frames, self.fps = frames, fps
        self.boot = int(time.time() * 1000)
        self.cond = threading.Condition()
        self.events, self.seq = [], 0
        self.state = {"awake": True, "app": "com.mediatek.wwtv.tvcenter", "muted": False, "volume": 20,
                      "channel": {"id": "1", "number": "1", "name": channel}}
        self.calls = []

    def event(self, type_, **fields):
        with self.cond:
            self.seq += 1
            self.events.append(dict(seq=self.seq, t=int(time.time() * 1000), type=type_, **fields))
            self.cond.notify_all()

    def jpeg(self, i):
        buf = io.BytesIO()
        Image.fromarray(self.frames[i % len(self.frames)]).convert("RGB").save(buf, "JPEG", quality=85)
        return buf.getvalue()


def handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = (json.dumps(body) + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            now = int(time.time() * 1000)
            if u.path == "/state":
                return self.send(200, dict(app.state, boot=app.boot, now=now, seq=app.seq, problems={}))
            if u.path == "/events":
                since, wait = int(q.get("since", 0)), min(int(q.get("wait", 0)), 55)
                with app.cond:
                    app.cond.wait_for(lambda: app.seq > since, timeout=wait)
                    evs = [e for e in app.events if e["seq"] > since]
                return self.send(200, dict(boot=app.boot, now=int(time.time() * 1000), seq=app.seq, events=evs))
            if u.path == "/status":
                return self.send(200, {"camera": "running", "last_error": ""})
            if u.path == "/shot.jpg":
                return self.send(200, app.jpeg(0), "image/jpeg")
            if u.path == "/stream.mjpg":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                i = 0
                try:
                    while True:
                        j = app.jpeg(i)
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(j))
                        self.wfile.write(j + b"\r\n")
                        i += 1
                        time.sleep(1.0 / app.fps)
                except (BrokenPipeError, ConnectionResetError):
                    return
            if u.path == "/mute":
                on = q.get("on", "1") != "0"
                app.calls.append({"t": now, "call": "mute", "on": on, "note": q.get("note")})
                if app.state["muted"] != on:
                    app.state["muted"] = on
                    app.event("mute", muted=on)
                return self.send(200, dict(app.state, boot=app.boot, now=now, seq=app.seq))
            if u.path == "/toast":
                app.calls.append({"t": now, "call": "toast", "text": q.get("text")})
                return self.send(200, b"ok\n", "text/plain")
            if u.path == "/_press":
                on = q.get("muted", "1") != "0"
                app.state["muted"] = on
                app.event("mute", muted=on)
                return self.send(200, {"muted": on})
            if u.path == "/_channel":
                app.state["channel"] = {"id": q["name"], "number": "1", "name": q["name"]}
                app.event("channel", **app.state["channel"])
                return self.send(200, app.state["channel"])
            if u.path == "/_log":
                return self.send(200, app.calls)
            self.send(404, {"error": "not found"})
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames")
    ap.add_argument("w", type=int)
    ap.add_argument("h", type=int)
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--fps", type=float, default=5)
    ap.add_argument("--channel", default="TEST CHANNEL")
    ap.add_argument("--count", type=int, default=300, help="frames to load")
    args = ap.parse_args()
    frames = np.fromfile(args.frames, dtype=np.uint8, count=args.w * args.h * args.count).reshape(-1, args.h, args.w)
    app = App(frames, args.fps, args.channel)
    print("fake TV app on 127.0.0.1:%d, %d frames" % (args.port, len(frames)), flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), handler(app)).serve_forever()


if __name__ == "__main__":
    main()
