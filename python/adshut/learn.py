#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Learns break markers from the user's own mutes and unmutes, for any channel.

Labels: every mute or unmute the user made by hand (not the detector's, not snapshots, not
accidental quick double presses), together with the channel it happened on.

  mute label:   the seconds before it are break material (opening ident, ads, trailers), which
                only airs inside breaks; it becomes "start" templates (mute markers).
  unmute label: the seconds before it show what comes back after a break (closing ident, station
                cards, the programme); it becomes "end" templates (unmute markers). Whether the
                station's trailers stay muted simply follows when the user unmutes.

Exact templates (corr.py): the window before the press is correlated against the recorded
history. Material that aired before shows up as diagonal runs whose ends are exactly where the
airings stop agreeing, so idents, cards and ads come out at their true length:

  mute:   every recurring segment in the 40 s before the press (all of it is break material)
  unmute: the last moving recurring segment in the 20 s before the press, on its own if its
          airings agree (below); if they contradict it, context is added step by step: the still
          cards right after it, then the segments before it ("closing ident -> logo card -> age
          card"), as far as needed to separate a return to the programme from the same material
          mid-break

Provisional templates: the first time something airs there is nothing to compare it with. The
press is then kept pending and the window is cut into overlapping 4 s pieces for the meantime.
Pending presses are retried on every run; once their material recurs (even in a break nobody
pressed anything in), exact templates are made and the provisional pieces are disabled.

Checks for every new template: still cards (station logo, age rating) are never markers on their
own, since they air on both sides of breaks; an unmute marker may end in them as context after a
moving ident. The other airings of a candidate are checked against the user's history: a mute
candidate that once aired shortly (60 s) before an unmute, or while the sound was on with no mute
following (programme), is dropped; so is an unmute candidate that aired shortly before a mute or
deep inside a muted break (an ad).

Validation: every run re-checks all enabled templates against the whole recording with the same
rules, so material that turns out to air somewhere it should not (a show's intro that also ends
it, a programme shot that recurs) is disabled before it misfires. Stretches that also appear
in the programme watched before the press (a static studio shot, say) are dropped, so are ones
already known. One that matches a
template of the opposite kind is ambiguous: the longer of the two carries context and is kept,
the shorter one (the shared core) is dropped; equal lengths drop both.

Feedback: when the user reverses a detector action within REVERSE_S, the template that caused it
gets a strike, one that stood a success; a template with more strikes than successes is disabled.

    learn.py            process new labels and feedback since the last run
    learn.py --all      start over: reprocess every label in the recording
    learn.py --dry-run  show what would be learned, change nothing
"""
import argparse
import json
import os
import time
from datetime import datetime

import numpy as np

from adshut import corr
from adshut import fp
from adshut import match
from adshut.library import Library

TOGGLE_S = 5            # a press undone within this long was accidental
WINDOW_S = 10.0         # seconds before the press that are cut into templates
SKIP_S = 0.4            # the last moment before the press (the press itself may be late or early)
PIECE_S = 4.0           # template length
STRIDE_S = 2.0          # step between overlapping templates
THRESHOLD = 0.85        # learned templates fire only on close matches
DUPLICATE = 0.85        # a window piece that an existing template already matches is not added
PROGRAMME_S = (30, 600)  # look this far back (after, before) for programme the piece must not match
MIN_WEIGHT = 0.4        # mostly featureless pieces (black, one colour) are useless
REDUNDANT_S = 60        # a label the detector already acted on this recently teaches nothing
REVERSE_S = 30          # a user press this soon after a detector action reverses it
STRIKES = 1             # a marker is dropped as soon as it has been reversed more often than it stood
EXACT_WINDOW_S = {"start": 40.0, "end": 20.0}
EXCLUDE_S = (150, 30)   # history left out around the press (before, after): its own break
HISTORY_DAYS = 14
PENDING_DAYS = 14
MAX_TEMPLATE_S = 10.0
TOUCH = 10              # grid frames (2 s): segments this close count as one stretch
CONTRA_S = 60           # an airing followed this soon by the opposite press contradicts a candidate

STATE = "learn_state.json"


def hms(t):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def labels(events):
    """User presses as dicts t, muted, channel, without accidental quick toggles."""
    acts = [e for e in events if e["type"] == "mute" and not e.get("first") and not e.get("by_detector")]
    keep = []
    for a in acts:
        if keep and a["t"] - keep[-1]["t"] < TOGGLE_S and a["muted"] != keep[-1]["muted"]:
            keep.pop()
        else:
            keep.append(a)
    return keep


def muted_spans(events):
    """(start, end) epochs of the times the sound was off, from every mute event."""
    spans, since = [], None
    for e in sorted((e for e in events if e["type"] == "mute"), key=lambda e: e["t"]):
        if e["muted"] and since is None:
            since = e["t"]
        elif not e["muted"] and since is not None:
            spans.append((since, e["t"]))
            since = None
    if since is not None:                      # still muted now
        spans.append((since, float("inf")))
    merged = []                                # a short burst of sound inside a break (a false
    for s0, s1 in spans:                       # unmute, re-muted soon) does not split the break
        if merged and s0 - merged[-1][1] < CONTRA_S:
            merged[-1] = (merged[-1][0], s1)
        else:
            merged.append((s0, s1))
    return merged


def detector_actions(events):
    return [e for e in events if e["type"] == "detector" and e.get("action") in ("mute", "unmute")
            and e.get("mode", "active") == "active"]


class Learner:
    def __init__(self, data=fp.DATA, dry_run=False, templates_dir=None):
        self.data = data
        self.dry = dry_run
        self.lib = Library(data, templates_dir)
        self.state_file = os.path.join(data, STATE)
        try:
            with open(self.state_file) as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {"labels_done": 0.0, "feedback_done": 0.0}
        self.timelines = {}

    def timeline(self, day):
        if day not in self.timelines:
            self.timelines[day] = fp.Timeline(fp.load_records(day, self.data))
        return self.timelines[day]

    def run(self):
        events = fp.load_events(self.data)
        now = time.time()
        # A label is only processed once its window is safely recorded and its toggle check is final.
        ready = now - TOGGLE_S - 5
        out = []
        dets = detector_actions(events)
        self.user = labels(events)
        self.spans = muted_spans(events)
        self.recorded_until = ready
        pending = [p for p in self.state.get("pending", []) if now - p["t"] < PENDING_DAYS * 86400]
        still = []
        for p in pending:
            r = self.learn_from(p, first=False)
            if r and r["resolved"]:
                out.append(r)
            elif not (r and "window not recorded" in r["skipped"]):   # nothing to learn from, ever
                still.append(p)
        for lab in labels(events):
            if not self.state["labels_done"] < lab["t"] <= ready or not lab.get("channel"):
                continue
            if any(d["action"] == ("mute" if lab["muted"] else "unmute") and 0 <= lab["t"] - d["t"] < REDUNDANT_S
                   for d in dets):
                continue
            r = self.learn_from(lab, first=True)
            if r:
                out.append(r)
                if not r["resolved"] and "window not recorded" not in r["skipped"]:
                    still.append({"t": lab["t"], "muted": lab["muted"], "channel": lab["channel"]})
        self.feedback(events, ready)
        self.disabled = self.validate()
        if not self.dry:
            self.state["labels_done"] = max(self.state["labels_done"], ready)
            self.state["pending"] = still
            with open(self.state_file, "w") as f:
                json.dump(self.state, f)
        return out

    def history(self):
        days = sorted(f[:-4] for f in os.listdir(os.path.join(self.data, "fp")) if f.endswith(".bin"))
        return [self.timeline(d) for d in days[-HISTORY_DAYS:]]

    def learn_from(self, lab, first):
        """Exact templates if the material recurs; else, on first sight, provisional pieces."""
        t, channel = lab["t"], lab["channel"]
        kind = "start" if lab["muted"] else "end"
        tl = self.timeline(datetime.fromtimestamp(t).strftime("%Y-%m-%d"))
        if not len(tl):
            return None
        result = {"label": "%s %s %s" % (hms(t), "mute" if lab["muted"] else "unmute", channel),
                  "kind": kind, "added": [], "skipped": [], "resolved": False}
        found = self.exact(lab, tl, kind, result)
        # Resolved once the material yielded a template (new or already known); a window whose
        # recurring parts were all rejected stays pending and keeps its provisional pieces.
        if found and (result["added"] or any(" already known " in x for x in result["skipped"])):
            result["resolved"] = True
            provisional = [e["name"] for e in self.lib.entries
                           if e.get("label_t") == t and e.get("provisional") and e.get("enabled", True)]
            for name in provisional:
                result["skipped"].append(name + " superseded")
                if not self.dry:
                    self.disable(name, "superseded by exact templates")
        elif first and not any(e.get("label_t") == t for e in self.lib.entries):
            self.pieces(lab, tl, kind, result)
        return result

    def exact(self, lab, tl, kind, result):
        """Adds exact templates from recurring material; True if the window had any."""
        t = lab["t"]
        s0 = max(0, tl.index(t - EXACT_WINDOW_S[kind]))
        s1 = min(len(tl) - 1, tl.index(t - SKIP_S))
        if s1 - s0 < corr.MIN_SEGMENT:
            return False
        win = slice(s0, s1 + 1)
        ok = tl.valid[win] & (tl.weight[win] >= corr.USABLE_WEIGHT)
        if ok.mean() < 0.5:
            result["skipped"].append("window not recorded")
            return False
        found = corr.recurrences(tl.feat[win], ok, self.history(), (t - EXCLUDE_S[0], t + EXCLUDE_S[1]))
        segs = corr.segments(s1 - s0 + 1, found)
        if not segs:
            return False
        moving = [not corr.is_static(tl.feat[s0 + a:s0 + b + 1]) for a, b, c in segs]
        if kind == "start":
            picks = [seg for seg, mv in zip(segs, moving) if mv]
            for (a, b, c), mv in zip(segs, moving):
                if not mv:
                    result["skipped"].append("%s-%s still card" % (hms(tl.time(s0 + a)), hms(tl.time(s0 + b))))
        else:
            # The last moving stretch, with the cards that directly follow it as context.
            last = max((k for k, mv in enumerate(moving) if mv), default=None)
            if last is None:
                result["skipped"].append("only still cards recur")
                return False
            a, b, c = segs[last]
            options = [(a, b)]                       # least context first
            for nxt in segs[last + 1:]:              # then the still cards that follow it
                if nxt[0] - options[-1][1] > TOUCH:
                    break
                options.append((a, nxt[1]))
            k, (a, b) = last, options[-1]
            while k > 0 and a - segs[k - 1][1] <= TOUCH and b - segs[k - 1][0] + 1 <= MAX_TEMPLATE_S * fp.RATE:
                k -= 1                               # then the segments before it
                a = segs[k][0]
                options.append((a, b))
            a, b = next((o for o in options if not self.contradiction(kind, corr.airing_times(found, *o))),
                        options[-1])
            picks = [(a, b, c)]
        if not picks:
            return False
        cap = int(MAX_TEMPLATE_S * fp.RATE)
        for a, b, c in picks:
            if b - a + 1 > cap:                 # mute: detect early; unmute: keep the context next to the return
                a, b = (a, a + cap - 1) if kind == "start" else (b - cap + 1, b)
            self.consider(lab, tl, kind, s0 + a, s0 + b + 1, result, exact=True, recurrences=c,
                          airings=corr.airing_times(found, a, b))
        return True

    def pieces(self, lab, tl, kind, result):
        """Provisional overlapping pieces from the seconds right before the press."""
        t = lab["t"]
        m = int(PIECE_S * fp.RATE)
        end = t - SKIP_S
        while end - PIECE_S >= t - WINDOW_S - 1e-6:
            s = tl.index(end - PIECE_S)
            end -= STRIDE_S
            if s < 0 or s + m > len(tl):
                break
            self.consider(lab, tl, kind, s, s + m, result, exact=False)

    def contradiction(self, kind, airings):
        """Why the candidate's other airings speak against it, or None."""
        for a in airings:
            for u in self.user:
                if u["muted"] == (kind == "end") and a < u["t"] <= a + CONTRA_S:
                    return "aired at %s, %d s before a user %s" % (hms(a), u["t"] - a,
                                                                    "mute" if u["muted"] else "unmute")
            muted = any(s0 <= a <= s1 for s0, s1 in self.spans)
            if kind == "end" and any(s0 < a < s1 - CONTRA_S for s0, s1 in self.spans):
                return "aired at %s inside a muted break" % hms(a)
            if kind == "start" and not muted and a < self.recorded_until - CONTRA_S \
                    and not any(a < s0 <= a + CONTRA_S for s0, s1 in self.spans):
                return "aired at %s during the programme (sound on, no mute followed)" % hms(a)
        return None

    def validate(self):
        """Re-checks every enabled template's airings in the whole recording; returns what it disabled."""
        out = []
        timelines = self.history()
        for e in list(self.lib.entries):
            if not e.get("enabled", True) or e["kind"] not in ("start", "end"):
                continue
            d = np.load(os.path.join(self.lib.dir, e["name"] + ".npz"))
            airings = []
            for tl in timelines:
                if len(tl) < len(d["feat"]):
                    continue
                sc = match.robust_scores(d["feat"], d["weight"], tl.feat, tl.valid)
                for p in match.peaks(sc, e.get("threshold", THRESHOLD), int(10 * fp.RATE)):
                    t = tl.time(p)
                    if abs(t - e["source"]) > 5:
                        airings.append(t)
            why = self.contradiction(e["kind"], airings)
            if why:
                out.append("%s disabled: %s" % (e["name"], why))
                if not self.dry:
                    self.disable(e["name"], why)
        return out

    def consider(self, lab, tl, kind, s, e, result, exact, recurrences=0, airings=()):
        """Checks a stretch of the timeline and adds it as a template if it passes."""
        t, channel = lab["t"], lab["channel"]
        piece = slice(s, e)
        label = "%s-%s" % (hms(tl.time(s)), hms(tl.time(e - 1)))
        if tl.valid[piece].mean() < 0.9:
            result["skipped"].append(label + " not recorded")
            return
        why = self.contradiction(kind, airings)
        if why:
            result["skipped"].append(label + " contradicted: " + why)
            return
        f, w = tl.feat[piece], tl.weight[piece]
        if w.mean() < MIN_WEIGHT:
            result["skipped"].append(label + " featureless")
            return
        existing = self.lib.for_channel(channel)
        sims = [(self.similarity(f, w, ef, ew), en, len(ef)) for en, ef, ew in existing]
        for sc, en, n in sims:
            if sc < DUPLICATE or en["kind"] == kind:
                continue
            if n >= len(f) + fp.RATE:           # the existing one is longer: it carries context, ours is the core
                result["skipped"].append(label + " ambiguous: inside %s template %s" % (en["kind"], en["name"]))
                return
            why = "also seen before a user %s" % ("mute" if lab["muted"] else "unmute")
            if not self.dry:
                self.disable(en["name"], why)
            if len(f) < n + fp.RATE:            # about equal: both are just the shared core
                result["skipped"].append(label + " ambiguous: same as %s template %s, both dropped"
                                         % (en["kind"], en["name"]))
                return
            result["skipped"].append(en["name"] + " disabled: it is the core of this longer stretch")
        # Known only if an existing template of the same kind is no longer than this one: a longer
        # one needs context this one does not, so it does not cover it.
        dup = [en for sc, en, n in sims if sc >= DUPLICATE and en["kind"] == kind and n <= len(f) + fp.RATE
               and not (exact and en.get("provisional"))]
        if dup:
            result["skipped"].append(label + " already known (%s)" % dup[0]["name"])
            return
        p_end, p_start = tl.index(t - PROGRAMME_S[0]), max(0, tl.index(t - PROGRAMME_S[1]))
        if p_end - p_start > e - s:
            prog = float(match.scores(f, w, tl.feat[p_start:p_end], tl.valid[p_start:p_end]).max())
            if prog >= THRESHOLD - 0.1:
                result["skipped"].append(label + " also in the programme before (%.2f)" % prog)
                return
        how = "exact, %d other airing(s)" % recurrences if exact else "provisional"
        if self.dry:
            result["added"].append("%s %.1f s %s (dry run)" % (label, (e - s) / fp.RATE, how))
            return
        name = self.lib.add(channel, kind, tl.zones[piece], tl.time(s), origin="learned", threshold=THRESHOLD,
                            note="learned from user %s at %s (%s)" % ("mute" if lab["muted"] else "unmute",
                                                                      hms(t), how),
                            extra={"label_t": t, "provisional": not exact, "recurrences": recurrences})
        result["added"].append("%s %.1f s %s" % (name, (e - s) / fp.RATE, how))

    @staticmethod
    def similarity(f, w, ef, ew):
        """Best score of the shorter sequence anywhere along the longer one."""
        if len(ef) <= len(f):
            return float(match.scores(ef, ew, f, np.ones(len(f), bool)).max())
        return float(match.scores(f, w, ef, np.ones(len(ef), bool)).max())

    def disable(self, name, why):
        for e in self.lib.entries:
            if e["name"] == name and e.get("enabled", True):
                e["enabled"] = False
                e["note"] = (e.get("note", "") + " | disabled: " + why).strip(" |")
        self.lib.save()

    def feedback(self, events, ready):
        """Strikes for detector actions the user reversed, successes for the ones that stood."""
        user = labels(events)
        changed = False
        for d in detector_actions(events):
            if not self.state["feedback_done"] < d["t"] <= ready - REVERSE_S or not d.get("template"):
                continue
            entry = next((e for e in self.lib.entries if e["name"] == d["template"]), None)
            if entry is None:
                continue
            reversed_ = any(u["muted"] != (d["action"] == "mute") and 0 < u["t"] - d["t"] < REVERSE_S for u in user)
            if d["action"] == "unmute":       # sound back on, but muted again soon (by anyone): the break went on
                reversed_ = reversed_ or any(e["type"] == "mute" and e["muted"] and 0 < e["t"] - d["t"] < REVERSE_S
                                             and not e.get("first") for e in events)
            key = "strikes" if reversed_ else "successes"
            entry[key] = entry.get(key, 0) + 1
            changed = True
        for entry in self.lib.entries:      # also applies a changed rule to earlier counts
            if (entry.get("enabled", True) and entry.get("strikes", 0) >= STRIKES
                    and entry["strikes"] > entry.get("successes", 0)):
                entry["enabled"] = False
                entry["note"] = (entry.get("note", "") + " | disabled: reversed by the user %d times" %
                                 entry["strikes"]).strip(" |")
                changed = True
        if changed and not self.dry:
            self.lib.save()
        if not self.dry:
            self.state["feedback_done"] = max(self.state["feedback_done"], ready - REVERSE_S)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="reprocess every label in the recording")
    ap.add_argument("--data", default=fp.DATA, help="one TV's data (fp/, events.jsonl); default: $ADSHUT_DATA or ~/.local/share/adshut")
    ap.add_argument("--templates", help="shared template library (default: DATA/templates)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    learner = Learner(os.path.expanduser(args.data), dry_run=args.dry_run, templates_dir=args.templates)
    if args.all:
        learner.state = {"labels_done": 0.0, "feedback_done": 0.0}
    results = learner.run()
    for r in results:
        print("%s -> %d %s template(s)%s" % (r["label"], len(r["added"]), r["kind"],
                                             "" if r["resolved"] else ", pending"))
        for a in r["added"]:
            print("    + " + a)
        for n in r["skipped"]:
            print("    - " + n)
    for n in learner.disabled:
        print("validation: " + n)


if __name__ == "__main__":
    main()
