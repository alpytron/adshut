#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""adshut template server: hands the shared template library to TVs and collects their data.

TVs keep their own fingerprint and event logs and append them here with resumable uploads; the
learner (detector/learn.py) works on that data and writes the shared library, which every TV
downloads for the channel it is watching. Each TV has its own token, so more TVs (also outside
this home) can join without sharing a secret.

API (all requests need "Authorization: Bearer <token>"):
  GET /v1/templates[?channel=NAME]        {"version": V, "templates": [{name, kind, threshold,
                                          frames, channel}]}; ETag V, If-None-Match -> 304
  GET /v1/templates/NAME.zones            raw fingerprints: frames x 432 bytes (9x16 zones x Y,Cb,Cr)
  GET /v1/sync/TV                         {"fp": {"YYYY-MM-DD": bytes}, "events": bytes}: resume points
  PUT /v1/sync/TV/fp/YYYY-MM-DD?offset=N  append fingerprint records (440 bytes each) at offset N
  PUT /v1/sync/TV/events?offset=N         append event log lines (JSON per line) at offset N
A TV may only write its own data. Uploads must start at or before the current end (a resend of
data already stored is accepted and skipped), so a TV can simply retry after any failure.

Tokens: server.py --add-tv ID prints a new token for TV ID (stored in DATA/tokens.json).
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from adshut import fp

RECORD = 8 + 432
MAX_BODY = 32 * 1024 * 1024
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class Store:
    def __init__(self, data):
        self.data = os.path.expanduser(data)
        self.tpl = os.path.join(self.data, "templates")
        self.lock = threading.Lock()
        os.makedirs(os.path.join(self.data, "tvs"), exist_ok=True)

    # ------------------------------------------------------------ tokens

    def tokens(self):
        try:
            with open(os.path.join(self.data, "tokens.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def add_tv(self, tv):
        if not ID_RE.match(tv):
            raise SystemExit("TV id: lowercase letters, digits and dashes")
        toks = self.tokens()
        token = secrets.token_urlsafe(24)
        toks[tv] = hashlib.sha256(token.encode()).hexdigest()   # only the hash is kept
        path = os.path.join(self.data, "tokens.json")
        with open(path + ".tmp", "w") as f:
            json.dump(toks, f, indent=1)
        os.chmod(path + ".tmp", 0o600)
        os.replace(path + ".tmp", path)
        return token

    def who(self, token):
        h = hashlib.sha256(token.encode()).hexdigest()
        for tv, th in self.tokens().items():
            if secrets.compare_digest(th, h):
                return tv
        return None

    # ------------------------------------------------------------ templates

    def index(self, channel=None):
        try:
            with open(os.path.join(self.tpl, "index.json")) as f:
                entries = json.load(f)
        except (OSError, ValueError):
            entries = []
        out = [{"name": e["name"], "kind": e["kind"], "threshold": e.get("threshold", 0.8),
                "frames": e["frames"], "channel": e["channel"]}
               for e in entries if e.get("enabled", True) and e["kind"] in ("start", "end")
               and (channel is None or e["channel"] == channel)]
        version = hashlib.sha1(json.dumps(out, sort_keys=True).encode()).hexdigest()[:16]
        return version, out

    def zones(self, name):
        import numpy as np                                     # only needed here
        path = os.path.join(self.tpl, name + ".npz")
        return np.load(path)["zones"].astype(np.uint8).tobytes()

    # ------------------------------------------------------------ TV data

    def tvdir(self, tv):
        d = os.path.join(self.data, "tvs", tv)
        os.makedirs(os.path.join(d, "fp"), exist_ok=True)
        return d

    def status(self, tv):
        d = self.tvdir(tv)
        fp = {f[:-4]: os.path.getsize(os.path.join(d, "fp", f))
              for f in sorted(os.listdir(os.path.join(d, "fp"))) if f.endswith(".bin")}
        ev = os.path.join(d, "events.jsonl")
        return {"fp": fp, "events": os.path.getsize(ev) if os.path.exists(ev) else 0}

    def append(self, path, offset, body, unit=1):
        """Writes body at offset; the part before the current end must already be there."""
        with self.lock:
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if offset > size:
                return 409, {"error": "gap", "size": size}
            if offset % unit or len(body) % unit:
                return 400, {"error": "not whole records", "size": size}
            skip = size - offset                               # resent data already stored
            if skip < len(body):
                with open(path, "ab") as f:
                    f.write(body[skip:])
            return 200, {"size": max(size, offset + len(body))}


class Handler(BaseHTTPRequestHandler):
    server_version = "adshut/1"
    store = None

    def log_message(self, fmt, *args):
        print(time.strftime("%H:%M:%S"), self.address_string(), fmt % args, flush=True)

    def reply(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = (json.dumps(body, ensure_ascii=False) + "\n").encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def auth(self):
        h = self.headers.get("Authorization", "")
        tv = self.store.who(h[7:]) if h.startswith("Bearer ") else None
        if tv is None:
            self.reply(401, {"error": "unknown token"})
        return tv

    def do_GET(self):
        tv = self.auth()
        if tv is None:
            return
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/")]
        q = parse_qs(u.query)
        if parts == ["v1", "templates"]:
            version, idx = self.store.index(q.get("channel", [None])[0])
            if self.headers.get("If-None-Match") == version:
                return self.reply(304, headers={"ETag": version})
            return self.reply(200, {"version": version, "templates": idx}, headers={"ETag": version})
        if len(parts) == 3 and parts[:2] == ["v1", "templates"] and parts[2].endswith(".zones"):
            name = parts[2][:-6]
            if not NAME_RE.match(name):
                return self.reply(400, {"error": "bad name"})
            try:
                return self.reply(200, self.store.zones(name), "application/octet-stream")
            except OSError:
                return self.reply(404, {"error": "no such template"})
        if len(parts) == 3 and parts[:2] == ["v1", "sync"]:
            if parts[2] != tv:
                return self.reply(403, {"error": "not your TV"})
            return self.reply(200, self.store.status(tv))
        self.reply(404, {"error": "not found"})

    def do_PUT(self):
        tv = self.auth()
        if tv is None:
            return
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/")]
        q = parse_qs(u.query)
        if len(parts) < 4 or parts[:2] != ["v1", "sync"]:
            return self.reply(404, {"error": "not found"})
        if parts[2] != tv:
            return self.reply(403, {"error": "not your TV"})
        n = int(self.headers.get("Content-Length", "0"))
        if n > MAX_BODY:
            return self.reply(413, {"error": "too large"})
        body = self.rfile.read(n)
        try:
            offset = int(q.get("offset", ["-1"])[0])
        except ValueError:
            offset = -1
        if offset < 0:
            return self.reply(400, {"error": "offset missing"})
        d = self.store.tvdir(tv)
        if len(parts) == 5 and parts[3] == "fp" and DAY_RE.match(parts[4]):
            code, res = self.store.append(os.path.join(d, "fp", parts[4] + ".bin"), offset, body, RECORD)
        elif len(parts) == 4 and parts[3] == "events":
            code, res = self.store.append(os.path.join(d, "events.jsonl"), offset, body)
        else:
            return self.reply(404, {"error": "not found"})
        self.reply(code, res)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=fp.DATA)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--add-tv", metavar="ID", help="create a token for TV ID, print it and exit")
    args = ap.parse_args()
    store = Store(args.data)
    if args.add_tv:
        print(store.add_tv(args.add_tv))
        return
    Handler.store = store
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print("adshut server on %s:%d, data %s" % (args.bind, args.port, store.data), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
