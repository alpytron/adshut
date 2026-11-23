# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Data behind the template server's recording viewer (web/viewer.html).

A "client" is a TV that uploads to this server (DATA/tvs/<id>), together with the older
recordings its learner reads as history (--history in DATA/tvs/<id>/learn.args). The server's
own data directory is listed as client "_host" when it holds recordings no TV claims as history.

Everything here only reads. Programmes, muted spans and verdicts are derived from the event log
the same way the learner sees it:

  programme  a stretch of recording on one channel; gaps up to PROG_GAP_S (menus, the guide,
             another app in front) do not split it, a channel change or a longer gap does
  muted span sound off from one mute event to the next unmute, with who started and ended it
             (detector, user, failsafe, power) and the detector's reason
  verdict    places worth a look: a break the user had to mute (missed opening), a detector
             mute the user undid quickly (false mute), a break the user had to unmute or the
             failsafe ended (missed closing), a re-mute soon after a detector unmute (wrong unmute)
"""
import json
import os
import re
import struct
import threading
import time
from datetime import datetime, timedelta

import numpy as np

from adshut import fp
from adshut import match
from adshut import library
from adshut.library import Library

HOST = "_host"
ID_RE = re.compile(r"^(_host|[a-z0-9][a-z0-9-]{0,40})$")
PROG_GAP_S = 300          # recording gaps up to this long stay inside one programme
COVER_GAP_S = 2.0         # frames further apart than this leave a hole in the coverage
QUICK_S = 30              # a user press this soon after a detector action corrects it
FALSE_MUTE_S = 60
TOGGLE_S = 10             # a user mute shorter than this is a quick toggle, not a break
MAX_FRAMES = 6000         # /frames: at most 20 minutes at 5 fps per request
MAX_STRIP = 4000
MAX_SCORE_S = 45 * 60
_review_lock = threading.Lock()
STRIDE_TARGET = 250000    # strip statistics look at about this many frames at most


class Viewer:
    def __init__(self, data):
        self.data = os.path.expanduser(data)
        self.tpl = os.path.join(self.data, "templates")

    # ------------------------------------------------------------ clients and sources

    def _history(self, tvdir):
        try:
            with open(os.path.join(tvdir, "learn.args")) as f:
                args = f.read().split()
        except OSError:
            return []
        return [os.path.realpath(os.path.expanduser(args[i + 1]))
                for i, a in enumerate(args[:-1]) if a == "--history"]

    def _tvs(self):
        root = os.path.join(self.data, "tvs")
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return []
        return [n for n in names if ID_RE.match(n) and n != HOST and os.path.isdir(os.path.join(root, n, "fp"))]

    def sources(self, client):
        """Data directories of a client, own data first."""
        if not ID_RE.match(client or ""):
            return None
        if client == HOST:
            return [self.data] if os.path.isdir(os.path.join(self.data, "fp")) else None
        d = os.path.join(self.data, "tvs", client)
        if not os.path.isdir(os.path.join(d, "fp")):
            return None
        return [d] + [h for h in self._history(d) if os.path.isdir(os.path.join(h, "fp"))]

    def clients(self):
        out, claimed = [], set()
        for tv in self._tvs():
            src = self.sources(tv)
            claimed.update(src[1:])
            out.append(self._describe(tv, tv, src))
        if os.path.isdir(os.path.join(self.data, "fp")) and os.path.realpath(self.data) not in claimed:
            out.append(self._describe(HOST, "this server's own host", [self.data]))
        return out

    def _describe(self, cid, label, src):
        days = {}
        for d in src:
            for f in os.listdir(os.path.join(d, "fp")):
                if f.endswith(".bin") and re.match(r"^\d{4}-\d{2}-\d{2}\.bin$", f):
                    days[f[:-4]] = days.get(f[:-4], 0) + os.path.getsize(os.path.join(d, "fp", f)) // fp.REC.itemsize
        first = last = None
        for d in src:
            for f in sorted(x for x in os.listdir(os.path.join(d, "fp")) if x.endswith(".bin")):
                rec = np.memmap(os.path.join(d, "fp", f), dtype=fp.REC, mode="r")
                if len(rec):
                    first = min(first or 1e300, float(rec["t"][0]))
                    last = max(last or 0, float(rec["t"][-1]))
        return {"id": cid, "label": label, "history": len(src) > 1,
                "days": [{"day": k, "frames": v} for k, v in sorted(days.items())],
                "first": first, "last": last}

    # ------------------------------------------------------------ events

    def events(self, client):
        """Events like the learner sees them: history events only before the client's own start."""
        src = self.sources(client)
        own = _load_events(src[0])
        start = min((e["t"] for e in own), default=float("inf"))
        older = [e for d in src[1:] for e in _load_events(d) if e["t"] < start]
        return sorted(older + own, key=lambda e: e["t"])

    # ------------------------------------------------------------ fingerprints

    def records(self, client, t0, t1):
        """Fingerprint records with t0 <= t < t1, sorted by time (memory-mapped where possible)."""
        parts = []
        d0 = datetime.fromtimestamp(t0).date() - timedelta(days=1)
        d1 = datetime.fromtimestamp(t1).date() + timedelta(days=1)
        for src in self.sources(client):
            for f in sorted(os.listdir(os.path.join(src, "fp"))):
                if not f.endswith(".bin"):
                    continue
                try:
                    day = datetime.strptime(f[:-4], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if not d0 <= day <= d1:
                    continue
                rec = np.memmap(os.path.join(src, "fp", f), dtype=fp.REC, mode="r")
                t = rec["t"]
                a, b = np.searchsorted(t, t0), np.searchsorted(t, t1)
                if b > a:
                    parts.append(rec[a:b])
        if not parts:
            return np.zeros(0, fp.REC)
        if len(parts) == 1:
            return parts[0]
        r = np.concatenate(parts)
        return r[np.argsort(r["t"], kind="stable")]

    def coverage(self, client):
        """(start, end) runs of recording over all of the client's data."""
        runs = []
        for src in self.sources(client):
            for f in sorted(os.listdir(os.path.join(src, "fp"))):
                if f.endswith(".bin"):
                    t = np.asarray(np.memmap(os.path.join(src, "fp", f), dtype=fp.REC, mode="r")["t"])
                    if len(t):
                        runs.extend(_runs(t, COVER_GAP_S))
        runs.sort()
        merged = []
        for a, b in runs:
            if merged and a <= merged[-1][1] + COVER_GAP_S:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return merged

    def strip(self, client, t0, t1, n):
        """Overview of [t0, t1) in n buckets, packed little-endian:
        uint32 n, float32 cover[n], luma[n], spread[n], motion[n], uint8 zones[n][432]
        (cover = share of the bucket with frames, zones = the frame nearest the bucket centre)."""
        n = max(1, min(int(n), MAX_STRIP))
        rec = self.records(client, t0, t1)
        t = np.asarray(rec["t"])
        width = (t1 - t0) / n
        edges = t0 + width * np.arange(n + 1)
        counts = np.diff(np.searchsorted(t, edges))
        cover = np.clip(counts / max(width * fp.RATE, 1e-9), 0, 1).astype(np.float32)
        zones = np.zeros((n, 432), np.uint8)
        luma = np.zeros(n, np.float32)
        spread = np.zeros(n, np.float32)
        motion = np.zeros(n, np.float32)
        if len(t):
            centres = edges[:-1] + width / 2
            i = np.clip(np.searchsorted(t, centres), 1, max(len(t) - 1, 1))
            near = np.where(np.abs(t[i - 1] - centres) <= np.abs(t[i] - centres), i - 1, i) if len(t) > 1 else np.zeros(n, int)
            ok = np.abs(t[near] - centres) <= max(width / 2, 1.0)
            zones[ok] = np.asarray(rec["z"][near[ok]]).reshape(-1, 432)
            stride = max(1, len(t) // STRIDE_TARGET)
            sel = np.arange(0, len(t), stride)
            ts = t[sel]
            y = np.asarray(rec["z"][sel])[..., 0].reshape(len(sel), -1).astype(np.float32)
            b = np.clip(((ts - t0) / width).astype(int), 0, n - 1)
            cnt = np.bincount(b, minlength=n).astype(np.float32)
            has = cnt > 0
            luma[has] = (np.bincount(b, y.mean(axis=1), minlength=n)[has] / cnt[has])
            spread[has] = (np.bincount(b, y.std(axis=1), minlength=n)[has] / cnt[has])
            if len(sel) > 1:
                d = np.abs(np.diff(y, axis=0)).mean(axis=1)
                adj = np.diff(ts) <= stride / fp.RATE + 1.0
                db = b[1:][adj]
                dc = np.bincount(db, minlength=n).astype(np.float32)
                m = dc > 0
                motion[m] = np.bincount(db, d[adj], minlength=n)[m] / dc[m]
        return (struct.pack("<I", n) + cover.tobytes() + luma.astype("<f4").tobytes()
                + spread.astype("<f4").tobytes() + motion.astype("<f4").tobytes() + zones.tobytes())

    def frames(self, client, t0, t1):
        """Raw records (float64 epoch + 432 zone bytes) in [t0, t1), at most MAX_FRAMES."""
        rec = self.records(client, t0, t1)
        return np.ascontiguousarray(rec[:MAX_FRAMES]).tobytes()

    # ------------------------------------------------------------ templates

    def templates(self):
        try:
            with open(os.path.join(self.tpl, "index.json")) as f:
                entries = json.load(f)
        except (OSError, ValueError):
            return []
        return library.apply_review(entries, self.tpl)

    def review_list(self):
        """Template index for the review page: each entry with the client it was cut from (stored,
        or the client that recorded that moment) and what it did according to the detector logs."""
        entries = self.templates()
        cids = [c["id"] for c in self.clients()]
        cover = {c: self.coverage(c) for c in cids}
        stats = {}
        for c in cids:
            for e in self.events(c):
                if e["type"] != "detector" or not e.get("template"):
                    continue
                st = stats.setdefault(e["template"], {"hits": 0, "mutes": 0, "unmutes": 0, "last": None,
                                                      "best": None, "clients": []})
                if e.get("action") == "hit":
                    st["hits"] += 1
                    sc = e.get("score")
                    if isinstance(sc, (int, float)):
                        st["best"] = max(st["best"] or 0, sc)
                elif e.get("action") == "mute":
                    st["mutes"] += 1
                elif e.get("action") == "unmute":
                    st["unmutes"] += 1
                st["last"] = max(st["last"] or 0, e["t"])
                if c not in st["clients"]:
                    st["clients"].append(c)
        for e in entries:
            src = e.get("client")
            if not src and e.get("source"):
                src = next((c for c in cids if any(a <= e["source"] <= b for a, b in cover[c])), None)
            e["source_client"] = src
            e["activity"] = stats.get(e["name"], {"hits": 0, "mutes": 0, "unmutes": 0, "last": None,
                                                  "best": None, "clients": []})
        return entries

    def template_zones(self, name):
        if not re.match(r"^[A-Za-z0-9._-]{1,120}$", name):
            return None
        try:
            return np.load(os.path.join(self.tpl, name + ".npz"))["zones"].astype(np.uint8).tobytes()
        except (OSError, KeyError, ValueError):
            return None

    def template_hits(self, name):
        """Every detector log line naming the template, newest first, with its client."""
        out = []
        for c in [c["id"] for c in self.clients()]:
            for e in self.events(c):
                if e["type"] == "detector" and e.get("template") == name:
                    out.append(dict(e, client=c))
        return sorted(out, key=lambda e: -e["t"])

    def set_review(self, name, enabled, note, by):
        """Stores (enabled True/False) or clears (None) an admin's decision about a template."""
        if not any(e["name"] == name for e in self.templates()):
            return False
        with _review_lock:
            review = library.load_review(self.tpl)
            if enabled is None:
                review.pop(name, None)
            else:
                review[name] = {"enabled": bool(enabled), "by": by, "t": round(time.time(), 1),
                                "note": str(note or "")[:500]}
            path = os.path.join(self.tpl, library.REVIEW)
            with open(path + ".tmp", "w") as f:
                json.dump(review, f, ensure_ascii=False, indent=1)
            os.replace(path + ".tmp", path)
        return True

    def scores(self, client, t0, t1, points=1500):
        """Best start- and end-template score over [t0, t1) for the channel on at each moment,
        max-pooled to at most `points` values; also the template behind each value."""
        t1 = min(t1, t0 + MAX_SCORE_S)
        events = self.events(client)
        lib = Library(self.data, self.tpl)
        pad = 30.0                                         # templates run past the window end
        rec = self.records(client, t0, t1 + pad)
        n_out = max(1, min(int(points), 4000))
        out = {"t0": t0, "t1": t1, "n": n_out, "start": [None] * n_out, "end": [None] * n_out,
               "start_tpl": [None] * n_out, "end_tpl": [None] * n_out, "names": [], "thresholds": {}}
        if len(rec) < 10:
            return out
        tl = fp.Timeline(np.asarray(rec))
        grid_t = tl.t0 + np.arange(len(tl)) / fp.RATE
        chan = np.array([_channel_at(events, x) or "" for x in grid_t[::25]], dtype=object)
        chan = np.repeat(chan, 25)[:len(tl)]
        names, best = [], {"start": None, "end": None}
        arg = {"start": None, "end": None}
        for c in sorted(set(chan) - {""}):
            mask = chan == c
            for e, tf, tw in lib.for_channel(c):
                if e["kind"] not in ("start", "end"):
                    continue
                s = match.robust_scores(tf, tw, tl.feat, tl.valid)
                s[~mask] = -1
                k = e["kind"]
                if best[k] is None:
                    best[k] = np.full(len(tl), -1.0, np.float32)
                    arg[k] = np.full(len(tl), -1, np.int32)
                better = s > best[k]
                best[k][better] = s[better]
                arg[k][better] = len(names)
                names.append(e["name"])
                out["thresholds"][e["name"]] = e.get("threshold", 0.8)
        out["names"] = names
        bins = np.clip(((grid_t - t0) / (t1 - t0) * n_out).astype(int), -1, n_out)
        inside = (bins >= 0) & (bins < n_out)
        for k in ("start", "end"):
            if best[k] is None:
                continue
            vals, tpl = out[k], out[k + "_tpl"]
            for i in np.nonzero(inside & (best[k] > -1))[0]:
                b = bins[i]
                if vals[b] is None or best[k][i] > vals[b]:
                    vals[b] = round(float(best[k][i]), 3)
                    tpl[b] = int(arg[k][i])
        return out

    # ------------------------------------------------------------ derived views

    def overview(self, client):
        """Everything the timeline draws from the event log, plus programmes and verdicts."""
        events = self.events(client)
        cover = self.coverage(client)
        spans = muted_spans(events)
        return {
            "client": client,
            "events": events,
            "coverage": cover,
            "power": _states(events, "power", lambda e: bool(e.get("awake"))),
            "apps": _states(events, "app", lambda e: e.get("app")),
            "channels": channel_spans(events),
            "muted": spans,
            "verdicts": verdicts(events, spans),
            "programmes": programmes(events, cover, spans),
        }


# ---------------------------------------------------------------- helpers (pure functions)

def _load_events(d):
    p = os.path.join(d, "events.jsonl")
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue                             # a half-written last line
                if isinstance(e, dict) and isinstance(e.get("t"), (int, float)) and e.get("type"):
                    out.append(e)
    except OSError:
        pass
    return out


def _runs(t, gap):
    breaks = np.nonzero(np.diff(t) > gap)[0]
    starts = np.concatenate([[0], breaks + 1])
    ends = np.concatenate([breaks, [len(t) - 1]])
    return [[float(t[a]), float(t[b]) + 1 / fp.RATE] for a, b in zip(starts, ends)]


def _channel_at(events, t):
    name = None
    for e in events:
        if e["t"] > t:
            break
        if e["type"] == "channel" and e.get("name"):
            name = e["name"]
        elif e["type"] in ("mute", "detector") and e.get("channel"):
            name = e["channel"]
    return name


def _states(events, kind, value):
    """[start, end, value] spans of a state carried by one event type (end None = still so)."""
    out = []
    for e in events:
        if e["type"] != kind:
            continue
        v = value(e)
        if out and out[-1][2] == v:
            continue
        if out:
            out[-1][1] = e["t"]
        out.append([e["t"], None, v])
    return out


def channel_spans(events):
    out = []
    for e in events:
        name = None
        if e["type"] == "channel":
            name = e.get("name")
        elif e["type"] in ("mute", "detector") and e.get("channel"):
            name = e["channel"]
        if not name or (out and out[-1][2] == name):
            continue
        if out:
            out[-1][1] = e["t"]
        out.append([e["t"], None, name])
    return out


def _detector_reason(events, t, action):
    """The detector's own log line for a mute change at t (it is written just before)."""
    for e in events:
        if e["type"] == "detector" and e.get("action") == action and abs(e["t"] - t) < 2.0:
            return e
    return None


def muted_spans(events):
    """Times the sound was off: start, end (None = still muted), started_by, ended_by,
    plus the templates and reasons the detector gave."""
    spans, cur = [], None
    for e in events:
        if e["type"] == "power" and not e.get("awake") and cur:
            cur.update(end=e["t"], ended_by="power")
            spans.append(cur)
            cur = None
            continue
        if e["type"] != "mute":
            continue
        if e["muted"] and cur is None:
            if e.get("first"):
                by = "state"                              # already muted when the log (re)started
            else:
                by = "detector" if e.get("by_detector") else "user"
            cur = {"start": e["t"], "end": None, "started_by": by, "channel": e.get("channel")}
            if by == "detector":
                d = _detector_reason(events, e["t"], "mute")
                if d:
                    cur.update(start_template=d.get("template"), start_reason=d.get("reason"))
        elif not e["muted"] and cur is not None:
            if e.get("by_detector"):
                d = _detector_reason(events, e["t"], "unmute")
                reason = (d or {}).get("reason") or ""
                by = "detector" if reason in ("", "unmute marker") else "failsafe"
                cur.update(end_template=(d or {}).get("template"), end_reason=reason or None)
            else:
                by = "state" if e.get("first") else "user"
            cur.update(end=e["t"], ended_by=by)
            spans.append(cur)
            cur = None
    if cur:
        spans.append(cur)
    return spans


def verdicts(events, spans):
    out = []
    for i, s in enumerate(spans):
        dur = (s["end"] or s["start"]) - s["start"]
        if s["started_by"] == "user" and dur >= TOGGLE_S:
            prev = spans[i - 1] if i else None
            if prev and prev["ended_by"] == "detector" and s["start"] - prev["end"] < QUICK_S:
                out.append({"t": s["start"], "kind": "wrong_unmute",
                            "text": "re-muted %.0f s after the detector unmuted (%s)"
                                    % (s["start"] - prev["end"], prev.get("end_template") or "?")})
            else:
                out.append({"t": s["start"], "kind": "missed_opening", "text": "the user muted this break"})
        if s["started_by"] == "detector" and s["ended_by"] == "user":
            if dur < FALSE_MUTE_S:
                out.append({"t": s["end"], "kind": "false_mute",
                            "text": "user unmuted %.0f s after a detector mute (%s)" % (dur, s.get("start_template") or "?")})
            else:
                out.append({"t": s["end"], "kind": "missed_closing", "text": "the user had to unmute"})
        if s["ended_by"] == "failsafe":
            out.append({"t": s["end"], "kind": "missed_closing",
                        "text": "failsafe: %s" % (s.get("end_reason") or "break too long")})
    return sorted(out, key=lambda v: v["t"])


def programmes(events, cover, spans):
    """Recorded stretches on one channel; gaps up to PROG_GAP_S stay inside one programme."""
    chans = channel_spans(events)

    def channel_of(t):
        name = None
        for a, _, c in chans:
            if a > t:
                break
            name = c
        return name

    # cut coverage runs at channel changes
    pieces = []
    for a, b in cover:
        cuts = [x[0] for x in chans if a < x[0] < b]
        edges = [a] + cuts + [b]
        for x, y in zip(edges[:-1], edges[1:]):
            if y - x > 0.5:
                pieces.append([x, y, channel_of(x + 0.01)])
    progs = []
    for a, b, c in pieces:
        p = progs[-1] if progs else None
        if p and p["channel"] == c and a - p["end"] <= PROG_GAP_S:
            p["end"] = b
            p["recorded"] += b - a
        else:
            progs.append({"start": a, "end": b, "channel": c, "recorded": b - a})
    for p in progs:
        inside = [s for s in spans if s["start"] < p["end"] and (s["end"] or 1e300) > p["start"]]
        p["muted"] = round(sum(min(s["end"] or p["end"], p["end"]) - max(s["start"], p["start"]) for s in inside), 1)
        p["breaks"] = len(inside)
        p["detector_mutes"] = sum(1 for s in inside if s["started_by"] == "detector")
        p["user_mutes"] = sum(1 for s in inside if s["started_by"] == "user")
        p["recorded"] = round(p["recorded"], 1)
    vs = verdicts(events, spans)
    for p in progs:
        p["issues"] = sum(1 for v in vs if p["start"] <= v["t"] <= p["end"])
    return progs
