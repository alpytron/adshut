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

Recording viewer: GET / (or /ui/) serves a web page that shows every TV's recorded fingerprints
and events on a timeline (web/viewer.html, data from viewer.py). Its API lives under /v1/admin/
and needs an admin token: server.py --add-admin NAME prints one (stored in DATA/admins.json).
Admins see all TVs' data; TV tokens never get access to it.
  GET /v1/admin/clients                          clients with their recording days
  GET /v1/admin/templates                        the whole template index (also disabled ones)
  GET /v1/admin/c/ID/overview                    events, coverage, programmes, muted spans, verdicts
  GET /v1/admin/c/ID/strip?from=T&to=T&n=N       overview strip (see viewer.Viewer.strip)
  GET /v1/admin/c/ID/frames?from=T&to=T          raw fingerprint records
  GET /v1/admin/c/ID/scores?from=T&to=T&n=N      best template scores over time
  GET /v1/admin/templates/review                 index with source client and detector activity
  GET /v1/admin/templates/NAME.zones             a template's fingerprints (also disabled ones)
  GET /v1/admin/templates/NAME/hits              detector log lines naming it, all clients
  POST /v1/admin/templates/NAME/review           {"enabled": true|false|null, "note": "..."}:
                                                 an admin decision that overrides the learner
                                                 (library.REVIEW); null clears it
Template review page: GET /ui/templates (web/templates.html).
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
from adshut import library
from adshut import viewer

RECORD = 8 + 432
MAX_BODY = 32 * 1024 * 1024
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
UI_PAGES = {"/ui/": "viewer.html", "/ui/templates": "templates.html"}
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class Store:
    def __init__(self, data):
        self.data = os.path.expanduser(data)
        self.tpl = os.path.join(self.data, "templates")
        self.lock = threading.Lock()
        os.makedirs(os.path.join(self.data, "tvs"), exist_ok=True)

    # ------------------------------------------------------------ tokens

    def tokens(self, file="tokens.json"):
        try:
            with open(os.path.join(self.data, file)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def add_tv(self, tv, file="tokens.json"):
        if not ID_RE.match(tv):
            raise SystemExit("id: lowercase letters, digits and dashes")
        toks = self.tokens(file)
        token = secrets.token_urlsafe(24)
        toks[tv] = hashlib.sha256(token.encode()).hexdigest()   # only the hash is kept
        path = os.path.join(self.data, file)
        with open(path + ".tmp", "w") as f:
            json.dump(toks, f, indent=1)
        os.chmod(path + ".tmp", 0o600)
        os.replace(path + ".tmp", path)
        return token

    def who(self, token, file="tokens.json"):
        h = hashlib.sha256(token.encode()).hexdigest()
        for tv, th in self.tokens(file).items():
            if secrets.compare_digest(th, h):
                return tv
        return None

    def admin(self, token):
        return self.who(token, "admins.json")

    # ------------------------------------------------------------ templates

    def index(self, channel=None):
        try:
            with open(os.path.join(self.tpl, "index.json")) as f:
                entries = json.load(f)
        except (OSError, ValueError):
            entries = []
        library.apply_review(entries, self.tpl)
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
    viewer = None

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
        u = urlparse(self.path)
        if u.path in ("/", "/ui"):
            return self.reply(302, b"", "text/plain", {"Location": "/ui/"})
        if u.path in UI_PAGES:
            with open(os.path.join(WEB, UI_PAGES[u.path]), "rb") as f:
                return self.reply(200, f.read(), "text/html; charset=utf-8",
                                  {"Cache-Control": "no-cache", "X-Frame-Options": "DENY",
                                   "Content-Security-Policy": CSP})
        if u.path.startswith("/v1/admin/"):
            return self.admin_get(u)
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

    def admin_who(self):
        h = self.headers.get("Authorization", "")
        who = self.store.admin(h[7:]) if h.startswith("Bearer ") else None
        if who is None:
            self.reply(401, {"error": "admin token needed"})
        return who

    def do_POST(self):
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/")]
        if parts[:2] != ["v1", "admin"]:
            return self.reply(404, {"error": "not found"})
        who = self.admin_who()
        if who is None:
            return
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > 65536:
            return self.reply(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.reply(400, {"error": "bad json"})
        if len(parts) == 5 and parts[2] == "templates" and parts[4] == "review" and isinstance(body, dict):
            enabled = body.get("enabled")
            if enabled not in (True, False, None):
                return self.reply(400, {"error": "enabled: true, false or null"})
            if not self.viewer.set_review(parts[3], enabled, body.get("note"), who):
                return self.reply(404, {"error": "no such template"})
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})

    def admin_get(self, u):
        if self.admin_who() is None:
            return
        parts = [unquote(p) for p in u.path.strip("/").split("/")][2:]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        v = self.viewer
        nostore = {"Cache-Control": "no-store"}
        try:
            if parts == ["clients"]:
                return self.reply(200, v.clients(), headers=nostore)
            if parts == ["templates"]:
                return self.reply(200, v.templates(), headers=nostore)
            if parts == ["templates", "review"]:
                return self.reply(200, v.review_list(), headers=nostore)
            if len(parts) == 2 and parts[0] == "templates" and parts[1].endswith(".zones"):
                z = v.template_zones(parts[1][:-6])
                if z is None:
                    return self.reply(404, {"error": "no such template"})
                return self.reply(200, z, "application/octet-stream", {"Cache-Control": "max-age=3600"})
            if len(parts) == 3 and parts[0] == "templates" and parts[2] == "hits":
                return self.reply(200, v.template_hits(parts[1]), headers=nostore)
            if len(parts) == 3 and parts[0] == "c":
                cid, what = parts[1], parts[2]
                if v.sources(cid) is None:
                    return self.reply(404, {"error": "no such client"})
                if what == "overview":
                    return self.reply(200, v.overview(cid), headers=nostore)
                t0, t1 = float(q["from"]), float(q["to"])
                if not t1 > t0:
                    return self.reply(400, {"error": "empty range"})
                if what == "strip":
                    return self.reply(200, v.strip(cid, t0, t1, int(q.get("n", 1000))),
                                      "application/octet-stream", nostore)
                if what == "frames":
                    return self.reply(200, v.frames(cid, t0, t1), "application/octet-stream", nostore)
                if what == "scores":
                    return self.reply(200, v.scores(cid, t0, t1, int(q.get("n", 1500))), headers=nostore)
        except (KeyError, ValueError):
            return self.reply(400, {"error": "bad parameters"})
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
    ap.add_argument("--add-admin", metavar="NAME",
                    help="create a token for the recording viewer (all TVs' data), print it and exit")
    args = ap.parse_args()
    store = Store(args.data)
    if args.add_tv:
        print(store.add_tv(args.add_tv))
        return
    if args.add_admin:
        print(store.add_tv(args.add_admin, "admins.json"))
        return
    Handler.store = store
    Handler.viewer = viewer.Viewer(store.data)
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print("adshut server on %s:%d, data %s" % (args.bind, args.port, store.data), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
