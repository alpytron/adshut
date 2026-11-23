// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.app;

import android.app.usage.UsageEvents;
import android.app.usage.UsageStatsManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.media.AudioManager;
import android.net.Uri;
import android.os.PowerManager;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Watches what the TV is doing and keeps a numbered list of changes, so a client on the network
 * needs no ADB: power, foreground app, mute/volume and the Live TV channel.
 *
 * Needs, granted once over ADB (see README): READ_LOGS for the channel (only the TV log reports
 * it), READ_TV_LISTINGS for channel names, usage access for the foreground app.
 *
 * Changes are also written to the DataLog in the recorder's event format (power, app, mute with
 * channel and by_detector, channel), and mute changes are passed to the Listener: a change the
 * app asked for itself (setMute(.., true)) is marked as ours, every other one is the user's.
 */
class TvWatch {
    interface Listener {
        void onMute(boolean muted, boolean ours, boolean first, double t);
    }

    private static final String TAG = "adshut";
    private static final int MAX_EVENTS = 500;
    private static final Pattern CHANNEL_ID =
            Pattern.compile("MSG_CHANNEL_CHANGED, channelUri = content://android\\.media\\.tv/channel/(\\d+)");

    private final Context ctx;
    private final AudioManager audio;
    private final PowerManager power;
    private final UsageStatsManager usage;
    private final SharedPreferences prefs;
    /** Changes whenever the app process restarts; event numbers start over with it. */
    final long boot = System.currentTimeMillis();

    private final ArrayList<JSONObject> events = new ArrayList<JSONObject>();
    private long seq;

    private Boolean awake, muted;
    private int volume = -1;
    private String app, activity, channelId, channelNumber, channelName;
    private DataLog dlog;
    private Listener listener;
    private Boolean expectMute;
    private long expectUntil;
    private String logError = "", usageError = "", listingError = "";
    private long lastUsageQuery;
    private volatile boolean running = true;
    private Process logcat;

    TvWatch(Context ctx) {
        this.ctx = ctx;
        audio = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
        power = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
        usage = (UsageStatsManager) ctx.getSystemService("usagestats");
        prefs = ctx.getSharedPreferences("tvwatch", Context.MODE_PRIVATE);
        // The log only reports the channel when it changes, so remember it across restarts.
        channelId = prefs.getString("channel", null);
        if (channelId != null) lookUpChannel();
    }

    void attach(DataLog dlog, Listener listener) {
        this.dlog = dlog;
        this.listener = listener;
    }

    synchronized boolean isAwake() {
        return awake != null && awake;
    }

    synchronized boolean isMuted() {
        return muted != null && muted;
    }

    synchronized String app() {
        return app;
    }

    synchronized String activity() {
        return activity;
    }

    /** Channel name, or "id N" while the name is unknown, or null before any channel was seen. */
    synchronized String channelName() {
        return channelName != null ? channelName : channelId != null ? "id " + channelId : null;
    }

    private void record(String type, Object... kv) {
        if (dlog == null) {
            return;
        }
        JSONObject o = new JSONObject();
        for (int i = 0; i + 1 < kv.length; i += 2) {
            if ("_".equals(kv[i])) {
                continue;                       // placeholder for a flag that does not apply
            }
            try {
                o.put((String) kv[i], kv[i + 1]);
            } catch (JSONException ignored) {
            }
        }
        dlog.event(System.currentTimeMillis() / 1000.0, type, o);
    }

    void start() {
        thread("adshut-poll", new Runnable() {
            @Override
            public void run() {
                pollLoop();
            }
        });
        thread("adshut-log", new Runnable() {
            @Override
            public void run() {
                logLoop();
            }
        });
    }

    void stop() {
        running = false;
        if (logcat != null) logcat.destroy();
    }

    private static void thread(String name, Runnable r) {
        Thread t = new Thread(r, name);
        t.setDaemon(true);
        t.start();
    }

    // ---------------------------------------------------------------- polling: power, app, sound

    private void pollLoop() {
        int n = 0;
        while (running) {
            try {
                boolean on = power.isInteractive();
                boolean mute = audio.isStreamMute(AudioManager.STREAM_MUSIC);
                int vol = audio.getStreamVolume(AudioManager.STREAM_MUSIC);
                String[] fg = n % 4 == 0 ? foregroundApp() : null;
                boolean muteChanged = false, ours = false, first = false;
                synchronized (this) {
                    if (awake == null || awake != on) {
                        record("power", "awake", on, awake == null ? "first" : "_", true);
                        awake = on;
                        event("power").put("awake", on);
                    }
                    if (muted == null || muted != mute) {
                        first = muted == null;
                        ours = expectMute != null && expectMute == mute && System.currentTimeMillis() < expectUntil;
                        if (ours) {
                            expectMute = null;
                        }
                        record("mute", "muted", mute, "channel", channelName() == null ? JSONObject.NULL : channelName(),
                                ours ? "by_detector" : "_", true, first ? "first" : "_", true);
                        muted = mute;
                        muteChanged = true;
                        event("mute").put("muted", mute);
                    }
                    if (vol != volume) {
                        volume = vol;
                        event("volume").put("volume", vol);
                    }
                    if (fg != null && (!fg[0].equals(app) || (fg[1] != null && !fg[1].equals(activity)))) {
                        record("app", "app", fg[0], "activity", fg[1] == null ? JSONObject.NULL : fg[1],
                                app == null ? "first" : "_", true);
                        app = fg[0];
                        activity = fg[1];
                        event("app").put("app", fg[0]).put("activity", fg[1] == null ? JSONObject.NULL : fg[1]);
                    }
                    notifyAll();
                }
                if (muteChanged && listener != null) {
                    listener.onMute(mute, ours, first, System.currentTimeMillis() / 1000.0);
                }
            } catch (Exception e) {
                Log.w(TAG, "poll: " + e);
            }
            n++;
            try {
                Thread.sleep(250);
            } catch (InterruptedException e) {
                return;
            }
        }
    }

    /** {package, activity class} most recently brought to the front, or null if nothing new. */
    private String[] foregroundApp() {
        long now = System.currentTimeMillis();
        // First look back far enough to find whatever is in front now; afterwards only new events.
        long from = lastUsageQuery == 0 ? now - 24L * 3600 * 1000 : lastUsageQuery - 2000;
        String[] latest = null;
        try {
            UsageEvents ev = usage.queryEvents(from, now + 1000);
            UsageEvents.Event e = new UsageEvents.Event();
            boolean any = false;
            while (ev.hasNextEvent()) {
                ev.getNextEvent(e);
                any = true;
                if (e.getEventType() == UsageEvents.Event.MOVE_TO_FOREGROUND) {
                    latest = new String[] {e.getPackageName(), e.getClassName()};
                }
            }
            usageError = lastUsageQuery == 0 && !any ? "no usage events: usage access not granted?" : "";
            lastUsageQuery = now;
        } catch (Exception e) {
            usageError = e.toString();
        }
        return latest;
    }

    // ---------------------------------------------------------------- TV log: channel changes

    private void logLoop() {
        boolean first = true;
        while (running) {
            try {
                // -e filters in logcat itself, so this costs next to nothing between channel changes.
                String[] cmd = first
                        ? new String[] {"logcat", "-d", "-e", "MSG_CHANNEL_CHANGED"}
                        : new String[] {"logcat", "-T", "1", "-e", "MSG_CHANNEL_CHANGED"};
                logcat = Runtime.getRuntime().exec(cmd);
                BufferedReader in = new BufferedReader(new InputStreamReader(logcat.getInputStream()));
                String line, last = null;
                int lines = 0;
                while ((line = in.readLine()) != null) {
                    lines++;
                    if (first) {
                        last = line;            // history: only the newest tune is current
                    } else {
                        onLogLine(line);
                    }
                }
                if (first) {
                    if (last != null) onLogLine(last);
                    // An app without READ_LOGS sees only its own lines, never the tuner's.
                    logError = lines == 0 && channelId == null ? "no tuner lines in the log: READ_LOGS not granted?" : "";
                    first = false;
                    continue;
                }
                logError = "logcat ended";
            } catch (Exception e) {
                logError = e.toString();
            }
            try {
                Thread.sleep(5000);
            } catch (InterruptedException e) {
                return;
            }
        }
    }

    private void onLogLine(String line) {
        Matcher m = CHANNEL_ID.matcher(line);
        if (!m.find()) return;
        String id = m.group(1);
        synchronized (this) {
            if (id.equals(channelId)) return;
            channelId = id;
            channelNumber = channelName = null;
            prefs.edit().putString("channel", id).apply();
        }
        lookUpChannel();
        synchronized (this) {
            record("channel", "id", channelId, "number", jsonOrNull(channelNumber), "name", jsonOrNull(channelName));
            try {
                event("channel").put("id", channelId).put("number", jsonOrNull(channelNumber))
                        .put("name", jsonOrNull(channelName));
            } catch (JSONException ignored) {
            }
            notifyAll();
        }
    }

    /** Number and name of the current channel from the TV's channel list. */
    private void lookUpChannel() {
        Cursor c = null;
        try {
            c = ctx.getContentResolver().query(Uri.parse("content://android.media.tv/channel/" + channelId),
                    new String[] {"display_number", "display_name"}, null, null, null);
            if (c != null && c.moveToFirst()) {
                channelNumber = c.getString(0);
                channelName = c.getString(1);
                listingError = "";
            } else {
                listingError = "channel not readable: READ_TV_LISTINGS not granted?";
            }
        } catch (Exception e) {
            listingError = e.toString();
        } finally {
            if (c != null) c.close();
        }
    }

    // ---------------------------------------------------------------- events and state

    /** Appends an event; caller holds the lock and adds its fields to the returned object. */
    private JSONObject event(String type) throws JSONException {
        JSONObject o = new JSONObject();
        o.put("seq", ++seq).put("t", System.currentTimeMillis()).put("type", type);
        events.add(o);
        if (events.size() > MAX_EVENTS) events.remove(0);
        return o;
    }

    private static Object jsonOrNull(Object o) {
        return o == null ? JSONObject.NULL : o;
    }

    /** Current state. "now" is this TV's clock, so a client can line event times up with its own. */
    synchronized JSONObject state() throws JSONException {
        JSONObject ch = new JSONObject();
        ch.put("id", jsonOrNull(channelId)).put("number", jsonOrNull(channelNumber)).put("name", jsonOrNull(channelName));
        JSONObject problems = new JSONObject();
        if (logError.length() > 0) problems.put("channel", logError);
        if (listingError.length() > 0) problems.put("channel_name", listingError);
        if (usageError.length() > 0) problems.put("app", usageError);
        return new JSONObject().put("boot", boot).put("now", System.currentTimeMillis()).put("seq", seq)
                .put("awake", jsonOrNull(awake)).put("app", jsonOrNull(app)).put("muted", jsonOrNull(muted))
                .put("volume", volume).put("channel", ch).put("problems", problems);
    }

    /** Events after {@code since}, waiting up to {@code waitMs} for one to happen. */
    synchronized JSONObject eventsSince(long since, long waitMs) throws JSONException {
        long deadline = System.currentTimeMillis() + waitMs;
        while (seq <= since) {
            long left = deadline - System.currentTimeMillis();
            if (left <= 0) break;
            try {
                wait(left);
            } catch (InterruptedException e) {
                break;
            }
        }
        JSONArray out = new JSONArray();
        for (JSONObject e : events) {
            if (e.getLong("seq") > since) out.put(e);
        }
        return new JSONObject().put("boot", boot).put("now", System.currentTimeMillis()).put("seq", seq)
                .put("events", out);
    }

    /** Mutes or unmutes the TV sound; ours marks the resulting change as the app's own. */
    void setMute(boolean on, boolean ours) {
        if (ours) {
            synchronized (this) {
                expectMute = on;
                expectUntil = System.currentTimeMillis() + 5000;
            }
        }
        setMute(on);
    }

    /** Mutes or unmutes the TV sound. */
    void setMute(boolean on) {
        audio.adjustStreamVolume(AudioManager.STREAM_MUSIC,
                on ? AudioManager.ADJUST_MUTE : AudioManager.ADJUST_UNMUTE, 0);
    }
}
