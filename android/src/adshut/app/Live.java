// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.app;

import android.media.Image;
import android.os.Handler;
import android.os.SystemClock;
import android.util.Log;

import org.json.JSONObject;

import java.nio.ByteBuffer;
import java.util.List;

import adshut.core.Action;
import adshut.core.Detector;
import adshut.core.Fingerprinter;
import adshut.core.ScreenTracker;

/**
 * The live loop on the TV: camera frame -> fingerprint -> screen tracking -> recording ->
 * detector -> mute and toast. Runs on the camera thread (background priority) and only while
 * the TV is awake with the Live TV screen itself in front; the EPG, menus that are activities
 * of their own, settings and other apps pause it.
 *
 * Mode (Config): off records only; shadow also runs the detector and logs/toasts what it would
 * do; active mutes and unmutes. Events go to DataLog in the recorder's format, so the learner
 * sees the same labels: the user's presses (mute events without by_detector) and the detector's
 * decisions.
 */
final class Live implements TvWatch.Listener, Sync.Listener {
    static final String TAG = "adshut";
    static final String LIVE_PKG = "com.mediatek.wwtv.tvcenter";
    static final String LIVE_ACTIVITY = "com.mediatek.wwtv.tvcenter.nav.TurnkeyUiMainActivity";
    static final long FRAME_MS = 200;                  // 5 fps, the recorder's rate

    interface Ui {
        void toast(String text);
    }

    private final Config cfg;
    private final TvWatch watch;
    private final DataLog log;
    private final Sync sync;
    private final Handler handler;
    private final Ui ui;

    private Fingerprinter fp;
    private ScreenTracker tracker;
    private final Detector det = new Detector();
    private final byte[] zones = new byte[432];
    private byte[] yBuf, uBuf, vBuf;

    private boolean capturing;
    private long frames, sessionFrames, lastFrameMs;
    private double avgMs;
    volatile String pausedBecause = "starting";

    Live(Config cfg, TvWatch watch, DataLog log, Sync sync, Handler handler, Ui ui) {
        this.cfg = cfg;
        this.watch = watch;
        this.log = log;
        this.sync = sync;
        this.handler = handler;
        this.ui = ui;
        tracker = new ScreenTracker(cfg.corners());
    }

    /** Corners changed by hand (/config): restart tracking from them. */
    void reloadCorners() {
        handler.post(new Runnable() {
            @Override
            public void run() {
                tracker = new ScreenTracker(cfg.corners());
                if (fp != null) {
                    fp.setCorners(tracker.corners());
                }
            }
        });
    }

    /** True when frames should be processed: TV awake, the Live TV screen itself in front. */
    boolean wanted() {
        String why = null;
        if (!watch.isAwake()) {
            why = "TV asleep";
        } else if (!LIVE_PKG.equals(watch.app())) {
            why = "not Live TV: " + watch.app();
        } else if (watch.activity() != null && !LIVE_ACTIVITY.equals(watch.activity())) {
            why = "Live TV covered by " + watch.activity();
        }
        pausedBecause = why == null ? "" : why;
        return why == null;
    }

    // ------------------------------------------------------------ frames (camera thread)

    /** One camera frame; returns quickly when the loop is paused or over its frame budget. */
    void onFrame(Image img) {
        double t = System.currentTimeMillis() / 1000.0;
        if (!wanted()) {
            stopCapture(t);
            return;
        }
        long now = SystemClock.elapsedRealtime();
        if (now - lastFrameMs < FRAME_MS - 10) {
            return;
        }
        lastFrameMs = now;
        long t0 = SystemClock.elapsedRealtime();
        try {
            int w = img.getWidth(), h = img.getHeight();
            if (fp == null || fp.width() != w / 2 || fp.height() != h / 2) {
                fp = new Fingerprinter(w / 2, h / 2);
                fp.setCorners(tracker.corners());
            }
            Image.Plane[] p = img.getPlanes();
            yBuf = copy(p[0].getBuffer(), yBuf);
            uBuf = copy(p[1].getBuffer(), uBuf);
            vBuf = copy(p[2].getBuffer(), vBuf);
            fp.load(yBuf, p[0].getRowStride(), uBuf, p[1].getRowStride(), p[1].getPixelStride(),
                    vBuf, p[2].getRowStride(), p[2].getPixelStride());
        } catch (Exception e) {
            Log.w(TAG, "frame: " + e);
            return;
        }
        if (!capturing) {
            capturing = true;
            sessionFrames = 0;
            log.event(t, "capture", put(new JSONObject(), "on", true));
        }
        ScreenTracker.Change c = tracker.add(fp.luma(), fp.width(), fp.height());
        if (tracker.status != null) {
            Log.i(TAG, "screen: " + tracker.status);
            tracker.status = null;
        }
        if (c != null) {
            fp.setCorners(c.corners);
            cfg.setCorners(c.corners);
            JSONObject o = new JSONObject();
            put(o, "change", c.kind);
            put(o, "corners", Config.cornersJson(c.corners));
            put(o, "previous", Config.cornersJson(c.previous));
            put(o, "shift", Math.round(c.shift * 10000) / 10000.0);
            log.event(t, "calibration", o);
        }
        fp.fingerprint(zones, 0);
        log.fingerprint(t, zones, 0);
        frames++;
        sessionFrames++;
        if (!"off".equals(cfg.mode())) {
            syncChannel(t);
            handle(det.push(t, zones, 0));
        }
        long ms = SystemClock.elapsedRealtime() - t0;
        avgMs = avgMs == 0 ? ms : 0.95 * avgMs + 0.05 * ms;
    }

    /** Regularly from the camera thread, also without frames: stalled picture, channel changes. */
    void tick() {
        double t = System.currentTimeMillis() / 1000.0;
        if (!wanted()) {
            stopCapture(t);
        }
        if (!"off".equals(cfg.mode())) {
            syncChannel(t);
            handle(det.tick(t));
        }
    }

    private void stopCapture(double t) {
        if (capturing) {
            capturing = false;
            log.event(t, "capture", put(put(new JSONObject(), "on", false), "frames", sessionFrames));
        }
    }

    private void syncChannel(double t) {
        String name = watch.channelName();
        if (name == null ? det.channel() != null : !name.equals(det.channel())) {
            handle(det.setChannel(name, sync.templates(name), t));
            sync.poke();
        }
    }

    private static byte[] copy(ByteBuffer b, byte[] into) {
        b.rewind();
        int n = b.remaining();
        if (into == null || into.length < n) {
            into = new byte[n];
        }
        b.get(into, 0, n);
        return into;
    }

    // ------------------------------------------------------------ decisions

    private void handle(List<Action> acts) {
        for (Action a : acts) {
            String ch = det.channel();
            if (Action.HIT.equals(a.type)) {
                JSONObject o = new JSONObject();
                put(o, "action", "hit");
                put(o, "template_kind", a.kind);
                put(o, "template", a.template);
                put(o, "score", Math.round(a.score * 1000) / 1000.0);
                put(o, "channel", ch);
                log.event(a.t, "detector", o);
                continue;
            }
            boolean mute = Action.MUTE.equals(a.type);
            String mode = cfg.mode();
            JSONObject o = new JSONObject();
            put(o, "action", a.type);
            put(o, "reason", a.reason);
            put(o, "mode", mode);
            put(o, "template", a.template == null ? JSONObject.NULL : a.template);
            put(o, "channel", ch);
            log.event(a.t, "detector", o);
            String note = cfg.toasts() ? toastText(mute, a.reason, ch) : null;
            if ("active".equals(mode)) {
                if (watch.isMuted() != mute) {
                    watch.setMute(mute, true);
                }
                if (note != null) {
                    ui.toast(note);
                }
            } else if (note != null) {
                ui.toast(note + " (test)");
            }
        }
    }

    /** Short enough to read in the few seconds a toast shows: icon, channel, what happened. */
    static String toastText(boolean mute, String reason, String channel) {
        String what = "mute marker".equals(reason) ? "opening"
                : "unmute marker".equals(reason) ? "closing"
                : "picture lost".equals(reason) ? "no picture"
                : "break too long".equals(reason) ? "timeout"
                : "channel changed".equals(reason) ? "channel change" : reason;
        String name = channel == null ? "" : channel.replaceAll("\\s+HD$", "").trim();
        return (mute ? "🔇 " : "🔊 ") + (name.length() > 0 ? name + " · " : "") + what;
    }

    // ------------------------------------------------------------ TV and server events (other threads)

    @Override
    public void onMute(final boolean muted, final boolean ours, final boolean first, final double t) {
        if (ours) {
            return;
        }
        handler.post(new Runnable() {
            @Override
            public void run() {
                // A snapshot that finds the sound off counts as a user mute: most likely a break the
                // user muted before the app (re)started.
                if (!first || muted) {
                    det.userOverride(muted, t);
                }
            }
        });
    }

    @Override
    public void templatesChanged() {
        handler.post(new Runnable() {
            @Override
            public void run() {
                det.setTemplates(sync.templates(det.channel()));
            }
        });
    }

    JSONObject status() {
        JSONObject o = new JSONObject();
        put(o, "processing", capturing);
        put(o, "paused_because", pausedBecause);
        put(o, "mode", cfg.mode());
        put(o, "frames", frames);
        put(o, "ms_per_frame", Math.round(avgMs * 10) / 10.0);
        put(o, "channel", det.channel() == null ? JSONObject.NULL : det.channel());
        put(o, "templates", sync.templates(det.channel()).size());
        put(o, "template_version", sync.version());
        put(o, "in_break", det.inBreak());
        put(o, "corners", Config.cornersJson(tracker.corners()));
        put(o, "sync", sync.lastResult);
        put(o, "sync_age_s", sync.lastOk == 0 ? -1 : (System.currentTimeMillis() - sync.lastOk) / 1000);
        return o;
    }

    static JSONObject put(JSONObject o, String k, Object v) {
        try {
            o.put(k, v);
        } catch (Exception ignored) {
        }
        return o;
    }
}
