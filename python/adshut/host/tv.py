# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""TV adapters: what the host knows about the TV and how it acts on it.

Every adapter offers
  recording_wanted()    True while frames should be processed (TV on, programme showing)
  channel_name()        current channel name, or a fixed label, or None
  muted                 the sound state as far as known (None = unknown)
  on_mute               set by the host: called with (muted, t) for the user's own presses
  set_mute(on, note)    act; note is a short text the TV may show (toast)
  toast(text)           show a short text, if the TV can
  run()                 blocking loop that follows the TV (run in a thread)

  androidtv-app  the adshut TV app over HTTP (state, events, mute, toasts)
  command        shell commands (IR blaster, HDMI-CEC, ...) for TVs that report nothing back
"""
import json
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from adshut.store import log


class AndroidTvApp:
    """Follows the TV through the AdShut app's HTTP interface (no ADB needed).

    /state gives a snapshot, /events?since=N&wait=S blocks until something changes. Events carry
    the TV's clock; each reply also carries the TV's "now", which is used to put them on ours.
    """

    def __init__(self, url, store, live_app="com.mediatek.wwtv.tvcenter"):
        self.base = url.rstrip("/")
        self.store = store
        self.live_app = live_app
        self.lock = threading.Lock()
        self.linked = False
        self.awake = self.app = self.muted = self.channel = None
        self.expect = None        # (muted, deadline): a change we asked for ourselves
        self.on_mute = None
        self.boot = None          # identifies the app process; event numbers restart with it
        self.seq = 0

    # ------------------------------------------------------------ adapter interface

    def recording_wanted(self):
        return self.linked and bool(self.awake) and self.app == self.live_app

    def channel_name(self):
        ch = self.channel
        return ch.get("name") if ch else None

    def set_mute(self, on, note=None):
        if self.muted == on:
            if note:
                self.toast(note)
            return
        self.expect = (on, time.time() + 5)
        self.call("/mute?on=%d" % on + ("&note=" + urllib.parse.quote(note) if note else ""))

    def toast(self, text):
        self.call("/toast?text=" + urllib.parse.quote(text))

    def call(self, path):
        try:
            urllib.request.urlopen(self.base + path, timeout=5).read()
        except Exception as e:
            self.store.event("detector_error", error=repr(e), path=path.split("?")[0])

    def run(self):
        while True:
            try:
                self.follow()
            except Exception as e:      # TV off the network, app not running, ...
                if self.linked:
                    log("tv:", repr(e))
            self.set_link(False)
            time.sleep(5)

    # ------------------------------------------------------------ following the app

    def channel_label(self):
        return self.channel and (self.channel.get("name") or "id %s" % self.channel.get("id"))

    def get(self, path, timeout):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return json.load(r)

    def set_link(self, up):
        if up != self.linked:
            self.linked = up
            self.store.event("tv_link", up=up)

    def follow(self):
        snap = self.get("/state", 8)
        if snap["boot"] != self.boot:
            # New app process (TV restarted, app updated): take its state as found.
            self.boot, self.seq = snap["boot"], snap["seq"]
            with self.lock:
                self.awake = self.app = self.muted = None
            self.apply(snap, None)
        self.set_link(True)
        for problem in snap.get("problems", {}).items():
            log("tv app problem: %s: %s" % problem)
        while True:
            reply = self.get("/events?since=%d&wait=25" % self.seq, 45)
            if reply["boot"] != self.boot:
                return                  # restarted meanwhile: start over with a snapshot
            offset = time.time() - reply["now"] / 1000.0
            for ev in reply["events"]:
                self.apply(ev, ev["t"] / 1000.0 + offset)
                self.seq = ev["seq"]

    def apply(self, d, t):
        """Feeds a snapshot (t None) or one event into the state."""
        new = {}
        if d.get("awake") is not None:
            new["awake"] = d["awake"]
        if d.get("app") is not None:
            new["app"] = d["app"]
        if d.get("muted") is not None:
            new["muted"] = d["muted"]
        ch = d.get("channel") if isinstance(d.get("channel"), dict) else d if d.get("type") == "channel" else None
        if ch and ch.get("id") is not None:
            new["channel"] = {"id": ch["id"], "number": ch.get("number"), "name": ch.get("name")}
        if new:
            # Channel first, so a mute event in the same batch is labelled with the right channel.
            for key in sorted(new, key=lambda k: k != "channel"):
                self.update(t, **{key: new[key]})

    def update(self, t=None, **new):
        """Applies reported values and logs an event for each one that changed.

        t is when it happened (our clock). Without t the value is a snapshot: it tells us the
        state, not the moment it changed, and the event is marked first=True.
        """
        with self.lock:
            for key, val in new.items():
                old = getattr(self, key)
                if val == old or val is None:
                    continue
                setattr(self, key, val)
                extra = {"first": True} if t is None else {}
                if key == "channel":
                    self.store.event("channel", t=t, **val, **extra)
                elif key == "muted":
                    ours = self.expect is not None and self.expect[0] == val and time.time() < self.expect[1]
                    if ours:
                        self.expect = None
                        extra["by_detector"] = True
                    self.store.event("mute", t=t, muted=val, channel=self.channel_label(), **extra)
                    if self.on_mute and not ours and (t is not None or val):
                        # A snapshot that finds the sound off counts as a user mute: most likely a
                        # break the user muted before the host (re)started.
                        self.on_mute(val, t if t is not None else time.time())
                elif key == "app":
                    self.store.event("app", t=t, app=val, **extra)
                elif key == "awake":
                    self.store.event("power", t=t, awake=val, **extra)


class CommandTv:
    """A TV controlled by shell commands: IR blasters (ir-ctl, LIRC irsend), HDMI-CEC
    (cec-client), smart plugs, ... For TVs that report nothing back.

    Config keys (lists are argv, strings run through the shell):
      mute, unmute      discrete commands, if the TV has them (best: no state to get wrong)
      toggle            the MUTE key, when that is all there is; the host then keeps the state it
                        believes the TV is in (it starts unmuted; correct it with the remote)
      toast             optional, "{text}" is replaced (e.g. a CEC on-screen-display string)
      power             optional check, exit code 0 = TV on; polled every POWER_POLL_S
      channel           fixed label for the template pool of this TV (default "tv")
      keys              optional user remote via a Linux input device (an IR receiver):
                        {"device": "/dev/input/by-path/...", "mute": 113}
    The user's MUTE presses flip the believed state and are logged as labels for the learner.
    The host's own IR transmissions, heard by a nearby receiver, are ignored for ECHO_S.
    """
    POWER_POLL_S = 30
    ECHO_S = 1.5

    def __init__(self, cfg, store):
        if not cfg.get("toggle") and not (cfg.get("mute") and cfg.get("unmute")):
            raise ValueError("command TV: give mute and unmute commands, or a toggle command")
        self.cfg, self.store = cfg, store
        self.label = cfg.get("channel", "tv")
        self.muted = False
        self.on_mute = None
        self.powered = None if cfg.get("power") else True
        self.last_sent = 0.0
        self.lock = threading.Lock()
        store.event("channel", id=self.label, number=None, name=self.label, first=True)
        store.event("mute", muted=False, channel=self.label, first=True, believed=True)

    def recording_wanted(self):
        return bool(self.powered)

    def channel_name(self):
        return self.label

    def run_cmd(self, cmd, **fmt):
        try:
            if isinstance(cmd, str):
                r = subprocess.run(cmd.format(**fmt), shell=True, capture_output=True, timeout=10)
            else:
                r = subprocess.run([c.format(**fmt) for c in cmd], capture_output=True, timeout=10)
            if r.returncode != 0:
                self.store.event("detector_error", error="command exit %d: %s" % (
                    r.returncode, r.stderr.decode(errors="replace").strip()[-200:]))
            return r.returncode == 0
        except Exception as e:
            self.store.event("detector_error", error=repr(e))
            return False

    def set_mute(self, on, note=None):
        with self.lock:
            if self.muted == on:
                if note:
                    self.toast(note)
                return
            cmd = self.cfg.get("mute" if on else "unmute") or self.cfg["toggle"]
            self.last_sent = time.time()
            if not self.run_cmd(cmd):
                return
            self.muted = on
            self.store.event("mute", muted=on, channel=self.label, by_detector=True)
        if note:
            self.toast(note)

    def toast(self, text):
        if self.cfg.get("toast"):
            self.run_cmd(self.cfg["toast"], text=text)

    def run(self):
        if self.cfg.get("keys"):
            threading.Thread(target=self.read_keys, daemon=True).start()
        while True:
            if self.cfg.get("power"):
                on = self.run_cmd(self.cfg["power"])
                if on != self.powered:
                    self.store.event("power", awake=on, **({"first": True} if self.powered is None else {}))
                    self.powered = on
            time.sleep(self.POWER_POLL_S)

    def read_keys(self):
        """The user's remote, through the input device an IR receiver creates (evdev events)."""
        k = self.cfg["keys"]
        mute_code = k.get("mute", 113)                     # KEY_MUTE
        ev = struct.Struct("llHHi")                       # struct input_event, native sizes
        while True:
            try:
                with open(k["device"], "rb") as f:
                    while True:
                        data = f.read(ev.size)
                        if len(data) < ev.size:
                            raise IOError("input device closed")
                        _, _, etype, code, value = ev.unpack(data)
                        if etype == 1 and code == mute_code and value == 1:
                            self.user_mute_key()
            except Exception as e:
                log("keys:", repr(e))
                time.sleep(5)

    def user_mute_key(self):
        t = time.time()
        if t - self.last_sent < self.ECHO_S:
            return                                         # our own transmission, heard back
        with self.lock:
            self.muted = not self.muted
            muted = self.muted
        self.store.event("mute", t=t, muted=muted, channel=self.label)
        if self.on_mute:
            self.on_mute(muted, t)
