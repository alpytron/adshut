// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.app;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Settings of the live loop, stored in the app's preferences and changed over HTTP (/config):
 *   server  template server base URL, e.g. http://192.168.1.10:8766
 *   tv      this TV's id at the server;  token  its bearer token (never shown back)
 *   mode    detector: off (record only), shadow (log decisions), active (mute and unmute)
 *   toasts  show a toast for each decision (in shadow mode marked "(test)")
 *   lan_frames  serve camera pictures (/shot.jpg, /stream.mjpg) on the local network; default
 *           off. Changed only on the TV's own settings screen, never over HTTP, so nobody on the
 *           network can switch it on: by default camera pictures never leave the TV.
 *   corners screen corners TL, TR, BR, BL as fractions of the camera frame (the tracker updates them)
 */
final class Config {
    private final SharedPreferences p;

    Config(Context ctx) {
        p = ctx.getSharedPreferences("live", Context.MODE_PRIVATE);
    }

    String server() {
        return p.getString("server", "");
    }

    String tv() {
        return p.getString("tv", "");
    }

    String token() {
        return p.getString("token", "");
    }

    String mode() {
        return p.getString("mode", "shadow");
    }

    boolean toasts() {
        return p.getBoolean("toasts", true);
    }

    boolean lanFrames() {
        return p.getBoolean("lan_frames", false);
    }

    /** Only for the on-screen settings (see the class comment); /config cannot set it. */
    void setLanFrames(boolean on) {
        p.edit().putBoolean("lan_frames", on).apply();
    }

    double[][] corners() {
        try {
            JSONArray a = new JSONArray(p.getString("corners", ""));
            double[][] c = new double[4][2];
            for (int i = 0; i < 4; i++) {
                c[i][0] = a.getJSONArray(i).getDouble(0);
                c[i][1] = a.getJSONArray(i).getDouble(1);
            }
            return c;
        } catch (Exception e) {
            return new double[][] {{0, 0}, {1, 0}, {1, 1}, {0, 1}};
        }
    }

    void setCorners(double[][] c) {
        p.edit().putString("corners", cornersJson(c).toString()).apply();
    }

    static JSONArray cornersJson(double[][] c) {
        JSONArray a = new JSONArray();
        for (double[] x : c) {
            JSONArray pt = new JSONArray();
            try {
                pt.put(Math.round(x[0] * 10000) / 10000.0).put(Math.round(x[1] * 10000) / 10000.0);
            } catch (Exception ignored) {
            }
            a.put(pt);
        }
        return a;
    }

    /** Applies one setting; returns an error text or null. */
    String set(String key, String value) {
        SharedPreferences.Editor e = p.edit();
        if (key.equals("server")) {
            String err = SettingsActivity.check(value.trim(), "");
            if (err != null) {
                return err;
            }
            e.putString("server", value.trim().replaceAll("/+$", ""));
        } else if (key.equals("tv")) {
            String err = SettingsActivity.check("", value.trim());
            if (err != null) {
                return err;
            }
            e.putString("tv", value.trim());
        } else if (key.equals("token")) {
            e.putString(key, value);
        } else if (key.equals("mode")) {
            if (!value.equals("off") && !value.equals("shadow") && !value.equals("active")) {
                return "mode is off, shadow or active";
            }
            e.putString("mode", value);
        } else if (key.equals("toasts")) {
            e.putBoolean("toasts", value.equals("1") || value.equals("true"));
        } else if (key.equals("corners")) {
            try {
                JSONArray a = new JSONArray(value);
                if (a.length() != 4) {
                    return "corners: four [x, y] pairs";
                }
                e.putString("corners", a.toString());
            } catch (Exception ex) {
                return "corners: " + ex.getMessage();
            }
        } else {
            return "unknown setting " + key;
        }
        e.apply();
        return null;
    }

    JSONObject toJson() {
        JSONObject o = new JSONObject();
        try {
            o.put("server", server()).put("tv", tv()).put("token_set", token().length() > 0)
                    .put("mode", mode()).put("toasts", toasts()).put("lan_frames", lanFrames()).put("corners", cornersJson(corners()));
        } catch (Exception ignored) {
        }
        return o;
    }
}
