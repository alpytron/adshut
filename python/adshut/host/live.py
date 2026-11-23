# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""The host's live loop: frames -> fingerprint -> screen tracking -> recording -> detector -> TV.

Capture pulls frames from a source while the TV adapter says a programme is showing; Breaks
connects the detector to the TV adapter (channel and the user's presses in, mute and toasts out).
In shadow mode decisions are only logged (and toasted with "(test)").
"""
import io
import json
import os
import re
import threading
import time

import numpy as np

from adshut.detect import Detector
from adshut.fingerprint import Fingerprinter, decode
from adshut.library import Library
from adshut.screen import ScreenTracker
from adshut.store import log

MAX_FPS = 5.0


class Breaks:
    def __init__(self, tv, store, library, mode="shadow", toasts=True):
        self.mode, self.toasts = mode, toasts
        self.tv, self.store, self.lib = tv, store, library
        self.lock = threading.Lock()
        self.det = Detector()
        self.channel = None
        self.lib_checked = 0.0
        tv.on_mute = self.on_user_mute

    def on_frame(self, t, zones):
        name = self.tv.channel_name()
        with self.lock:
            acts = []
            if name != self.channel:
                acts += self.det.set_channel(name, self.lib.for_channel(name) if name else [], t)
                self.channel = name
            if t - self.lib_checked > 30:
                self.lib_checked = t
                if self.lib.changed_on_disk():
                    self.lib.reload()
                    self.det.set_templates(self.lib.for_channel(self.channel) if self.channel else [])
                    log("detector: template library reloaded")
            acts += self.det.push(t, zones)
        self.handle(acts)

    def idle(self, t):
        with self.lock:
            acts = self.det.tick(t)
        self.handle(acts)

    def on_user_mute(self, muted, t):
        with self.lock:
            self.det.user_override(muted, t)

    def handle(self, acts):
        for a in acts:
            if a["action"] == "hit":
                self.store.event("detector", t=a["t"], action="hit", template_kind=a["kind"],
                                 template=a["template"], score=a["score"], channel=self.channel)
                continue
            mute = a["action"] == "mute"
            self.store.event("detector", t=a["t"], action=a["action"], reason=a["reason"], mode=self.mode,
                             template=a.get("template"), channel=self.channel)
            note = toast_text(mute, a["reason"], self.channel) if self.toasts else None
            if self.mode != "active":
                if note:
                    self.tv.toast(note + " (test)")
                continue
            self.tv.set_mute(mute, note)


def toast_text(mute, reason, channel):
    """Short enough to read in the few seconds a toast shows: icon, channel, what happened."""
    what = ("opening" if reason == "mute marker" else
            "closing" if reason == "unmute marker" else
            "no picture" if reason == "picture lost" else
            "timeout" if reason == "break too long" else
            "channel change" if reason == "channel changed" else reason)
    name = re.sub(r"\s+HD$", "", channel or "").strip()
    return "%s %s%s" % ("\U0001F507" if mute else "\U0001F50A", name + " · " if name else "", what)


class Capture:
    """Pulls frames while the TV shows a programme; stores fingerprints and review images."""

    def __init__(self, source, tv, store, breaks=None, corners=((0, 0), (1, 0), (1, 1), (0, 1)),
                 auto_calibrate=True, thumb_interval_s=1.0, thumb_quality=60):
        self.source, self.tv, self.store, self.breaks = source, tv, store, breaks
        self.auto_calibrate = auto_calibrate
        self.thumb_interval_s, self.thumb_quality = thumb_interval_s, thumb_quality
        self.last_thumb = 0.0
        self.last_frame_t = 0.0
        self.frames = 0
        self.last_error, self.last_error_t = None, 0.0
        self.calibration_file = os.path.join(store.dir, "calibration.json")
        if auto_calibrate:
            try:
                with open(self.calibration_file) as f:
                    corners = json.load(f)["corners"]
            except (OSError, ValueError, KeyError):
                pass
        self.tracker = ScreenTracker(corners)
        self.fingerprinter = Fingerprinter(self.tracker.corners)
        store.event("calibration", change="initial", corners=np.round(self.tracker.corners, 4).tolist())

    def run(self):
        while True:
            if not self.tv.recording_wanted():
                if self.breaks:
                    self.breaks.idle(time.time())   # programme left or TV off: a break in progress ends
                time.sleep(1)
                continue
            n0 = self.frames
            try:
                for t, jpeg in self.source.frames(self.tv.recording_wanted):
                    if t - self.last_frame_t < 1.0 / MAX_FPS - 0.01:
                        continue                    # the detector works on a 5 fps grid
                    self.last_frame_t = t
                    if self.frames == n0:
                        self.store.event("capture", on=True)
                        self.last_error = None
                    self.frame(t, jpeg)
            except Exception as e:  # network or camera trouble: note it and retry
                err = self.source.describe_error(repr(e))
                if err != self.last_error or time.time() - self.last_error_t > 600:
                    self.store.event("capture_error", error=err)   # once per distinct problem, not per retry
                    self.last_error, self.last_error_t = err, time.time()
                time.sleep(10)
            finally:
                if self.frames > n0:
                    self.store.event("capture", on=False, frames=self.frames - n0)

    def frame(self, t, jpeg):
        img = decode(jpeg, self.fingerprinter.rect_size)
        if self.auto_calibrate:
            change = self.tracker.add(img.getchannel(0))
            if self.tracker.status:
                log("screen:", self.tracker.status)
                self.tracker.status = None
            if change:
                self.fingerprinter.set_corners(self.tracker.corners)
                with open(self.calibration_file, "w") as f:
                    json.dump({"corners": change["corners"], "t": t}, f)
                self.store.event("calibration", **change)
        rect = self.fingerprinter.rectify(img)
        zones = self.fingerprinter.zones(rect)
        self.store.fingerprint(t, zones)
        self.frames += 1
        if self.breaks:
            try:
                self.breaks.on_frame(t, zones)
            except Exception as e:   # a detector fault must never stop the recording
                if not getattr(self, "detector_failed", False):
                    log("detector:", repr(e))
                    self.store.event("detector_error", error=repr(e))
                self.detector_failed = True
        if self.thumb_interval_s > 0 and t - self.last_thumb >= self.thumb_interval_s:
            self.last_thumb = t
            buf = io.BytesIO()
            rect.convert("RGB").save(buf, "JPEG", quality=self.thumb_quality)
            self.store.thumb(t, buf.getvalue())
