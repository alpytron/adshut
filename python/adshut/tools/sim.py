#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Causal simulation of break-detection policies over recorded days.

Every enabled template is matched offline against the recording to get its airings (hits).
The hits are then replayed in time order through a policy, with two rules that keep the replay
honest about what the live system could have known:

  - a template only exists once it would have been learned: ACTIVATE_S after the press it was
    learned from (learned templates) or after its source airing (hand-cut ones)
  - statistics about a template (what followed its airings) only use airings before the moment
    it is consulted, whose outcome was already known by then

Ground truth is the sound state the user ended up with: muted spans from all mute events
(short sound bursts inside a break merged), the same as the learner uses.

With --steady the replay estimates how things go once the library has matured: each airing is
judged with only the templates and statistics learned from *other* breaks (leave one break out,
LEAVE_OUT_S around the airing), instead of only what existed at the time.

    sim.py [DAY ...] [--policy kinds|glue] [--robust FRACTION] [--lookahead S] [--steady] [--verbose]
"""
import argparse
import os
from datetime import datetime

import numpy as np

from adshut import fp
from adshut import match
from adshut.learn import muted_spans
from adshut.library import Library

ACTIVATE_S = 180          # a learned template exists this long after its press (learner timer + reload)
COOLDOWN_S = 15
OUTCOME_S = (5, 20)       # what follows an airing: sound state this long after its end
MAX_BREAK_S = 8 * 60
LEAVE_OUT_S = 600
REACT_S = 60              # the user's press may lag the picture by this much


def hms(t):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def robust_scores(tf, tw, feat, valid, keep):
    if keep >= 1.0:
        return match.scores(tf, tw, feat, valid)
    return match.robust_scores(tf, tw, feat, valid, keep)


def usable(e, include_disabled):
    """Templates the simulation plays with: start/end ones, and disabled ones only on request
    (minus those superseded by exact templates, which were never meant to coexist with them)."""
    if e["kind"] not in ("start", "end"):
        return False
    if e.get("enabled", True):
        return True
    return include_disabled and "superseded" not in e.get("note", "")


def airings(lib, timelines, keep, include_disabled=False):
    """{template name: [(start epoch, end epoch, score)]} for every usable template."""
    out = {}
    for e in lib.entries:
        if not usable(e, include_disabled):
            continue
        d = np.load(os.path.join(lib.dir, e["name"] + ".npz"))
        m = len(d["feat"])
        hits = []
        for tl in timelines:
            if len(tl) < m:
                continue
            sc = robust_scores(d["feat"], d["weight"], tl.feat, tl.valid, keep)
            for p in match.peaks(sc, e.get("threshold", 0.8), int(COOLDOWN_S * fp.RATE)):
                hits.append((tl.time(p), tl.time(p + m - 1), float(sc[p])))
        out[e["name"]] = sorted(hits)
    return out


class Truth:
    def __init__(self, spans, record_times):
        self.spans = spans
        self.rt = record_times

    def recorded(self, t0, t1):
        return np.count_nonzero((self.rt >= t0) & (self.rt <= t1)) > 0.5 * (t1 - t0) * fp.RATE * 0.9

    def muted(self, t):
        return any(s0 <= t <= s1 for s0, s1 in self.spans)

    def outcome(self, end_t):
        """'programme' or 'break': what the user made of the time after an airing.

        Presses lag the picture (up to ~45 s seen), so the sound state right after an airing is
        corrected by the next press: sound on but muted within REACT_S -> a break followed;
        sound off but unmuted within REACT_S -> the programme followed.
        """
        muted = self.muted(end_t + OUTCOME_S[0])
        if muted:
            ends = [s1 for s0, s1 in self.spans if s0 <= end_t + OUTCOME_S[0] <= s1]
            return "programme" if ends and ends[0] - end_t <= REACT_S else "break"
        return "break" if any(end_t < s0 <= end_t + REACT_S for s0, s1 in self.spans) else "programme"


def simulate(policy, events_hits, entries, truth, lookahead, steady=False):
    """Runs hits through a policy; returns the list of (t, action, template, why)."""
    actions = []
    in_break, since, pending, last_fired = False, None, None, {}
    history = {name: [] for name in entries}          # (end time, outcome) of earlier airings
    every = {name: [(t, truth.outcome(t)) for s, t, n, sc in events_hits if n == name] for name in entries}

    def stats(name, now):
        if steady:     # all airings except the ones around this moment (another break's knowledge)
            past = [o for t_end, o in every[name] if abs(t_end - now) > LEAVE_OUT_S]
        else:
            past = [o for t_end, o in history[name] if t_end + OUTCOME_S[1] < now]
        return sum(o == "programme" for o in past), len(past)

    for t_start, t, name, score in events_hits:
        e = entries[name]
        if pending is not None and t > pending[0]:
            actions.append((pending[0], "unmute", pending[1], "lookahead passed"))
            in_break, since, pending = False, None, None
        if in_break and since is not None and t - since > MAX_BREAK_S:
            actions.append((since + MAX_BREAK_S, "unmute", None, "break too long"))
            in_break, since, pending = False, None, None
        origin = e.get("label_t") or e["source"]
        active = abs(t - origin) > LEAVE_OUT_S if steady else t >= origin + ACTIVATE_S
        if active and t - last_fired.get(name, -1e9) >= COOLDOWN_S:
            last_fired[name] = t
            if policy == "kinds":
                if e["kind"] == "start" and not in_break:
                    in_break, since = True, t
                    actions.append((t, "mute", name, "start template"))
                elif e["kind"] == "end" and in_break and t - since > 10:
                    in_break, since = False, None
                    actions.append((t, "unmute", name, "end template"))
            else:
                prog, n = stats(name, t)
                p = (prog + 0.5) / (n + 1)                 # with no history: undecided (0.5)
                if not in_break:
                    if p <= 0.35 or (n == 0 and e["kind"] == "start"):
                        in_break, since, pending = True, t, None
                        actions.append((t, "mute", name, "glue, programme followed %d/%d" % (prog, n)))
                elif t - since > 10:
                    if p >= 0.8 and n >= 2 or (n == 0 and e["kind"] == "end"):
                        in_break, since, pending = False, None, None
                        actions.append((t, "unmute", name, "programme followed %d/%d" % (prog, n)))
                    elif p > 0.35:
                        pending = (t + lookahead, name)    # mixed: unmute unless more break glue follows
                    else:
                        pending = None                     # break glue: the break goes on
        history[name].append((t, truth.outcome(t)))
    if pending is not None:
        actions.append((pending[0], "unmute", pending[1], "lookahead passed"))
    return actions


def report(actions, truth, user, verbose):
    """Per break: detector vs user; plus false mutes and false unmutes."""
    rows, false_mute, false_unmute = [], 0, 0
    for t, act, name, why in actions:
        muted_now = truth.muted(t)
        if act == "mute" and not muted_now and not any(t < s0 <= t + 60 for s0, s1 in truth.spans):
            false_mute += 1
            rows.append((t, "FALSE MUTE", name, why))
        elif act == "unmute" and any(s0 + 5 < t < s1 - 60 for s0, s1 in truth.spans):
            false_unmute += 1
            rows.append((t, "FALSE UNMUTE", name, why))
        elif verbose:
            rows.append((t, act, name, why))
    print("  breaks (user's sound state)        detector mute   detector unmute")
    for s0, s1 in truth.spans:
        if s1 - s0 < 10 or not truth.recorded(s0, min(s1, s0 + 60)):
            continue                                   # a test toggle, or no picture to judge by
        m = [t for t, a, n, w in actions if a == "mute" and s0 - 60 <= t <= s1]
        u = [t for t, a, n, w in actions if a == "unmute" and s0 + 5 <= t <= s1 + 60]
        print("  %s - %s  %-6s  %15s  %15s" % (hms(s0), hms(s1) if s1 < 1e12 else "now", "%ds" % min(s1 - s0, 9999),
                                             "%+.0fs" % (m[0] - s0) if m else "missed",
                                             "%+.0fs" % (u[-1] - s1) if u else "missed"))
    for t, kind, name, why in sorted(rows):
        print("  %s %-12s %s (%s)" % (hms(t), kind, name, why))
    print("  false mutes %d, false unmutes %d" % (false_mute, false_unmute))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("days", nargs="*")
    ap.add_argument("--policy", choices=("kinds", "glue"), default="glue")
    ap.add_argument("--robust", type=float, default=1.0, help="score on the best fraction of frames")
    ap.add_argument("--lookahead", type=float, default=7.0)
    ap.add_argument("--steady", action="store_true", help="leave-one-break-out estimate of a mature library")
    ap.add_argument("--with-disabled", action="store_true",
                    help="also play templates that validation or feedback disabled (tests a policy without those safeguards)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    days = args.days or [datetime.now().strftime("%Y-%m-%d")]
    timelines = [fp.Timeline(fp.load_records(d)) for d in days]
    events = fp.load_events()
    lib = Library()
    entries = {e["name"]: e for e in lib.entries if usable(e, args.with_disabled)}
    hits = airings(lib, timelines, args.robust, args.with_disabled)
    stream = sorted((s, t, name, sc) for name, hs in hits.items() for s, t, sc in hs)
    truth = Truth(muted_spans(events), np.concatenate([fp.load_records(d)["t"] for d in days]))
    actions = simulate(args.policy, stream, entries, truth, args.lookahead, args.steady)
    print("policy %s, robust %.2f, lookahead %.0fs, %s, %d templates, %d airings" % (
        args.policy, args.robust, args.lookahead, "steady state" if args.steady else "causal",
        len(entries), len(stream)))
    report(actions, truth, None, args.verbose)


if __name__ == "__main__":
    main()
