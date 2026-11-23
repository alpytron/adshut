// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.app;

import android.util.Log;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.text.SimpleDateFormat;
import java.util.Arrays;
import java.util.Date;
import java.util.Locale;

/**
 * The app's own recording, in the formats of the Python host (spec/README.md) so the learner, simulator and
 * replay tools work on it unchanged:
 *
 *   fp/YYYY-MM-DD.bin  records of float64 epoch seconds (little-endian) + 432 zone bytes
 *   events.jsonl       {"t": epoch s, "ts": local ISO time, "type": ..., ...} per line
 *
 * Fingerprints are kept FP_DAYS days and at most FP_MAX_BYTES in total (oldest days go first);
 * Sync uploads both to the template server.
 */
final class DataLog {
    static final String TAG = "adshut";
    static final int RECORD = 8 + 432;
    static final int FP_DAYS = 4;
    static final long FP_MAX_BYTES = 300L * 1024 * 1024;

    final File dir, fpDir, events;
    private final SimpleDateFormat day = new SimpleDateFormat("yyyy-MM-dd", Locale.US);
    private final SimpleDateFormat iso = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US);
    private String openDay;
    private FileOutputStream fpOut;
    private final ByteBuffer rec = ByteBuffer.allocate(RECORD).order(ByteOrder.LITTLE_ENDIAN);

    DataLog(File base) {
        dir = base;
        fpDir = new File(base, "fp");
        events = new File(base, "events.jsonl");
        fpDir.mkdirs();
        prune();
    }

    /** One fingerprint (432 bytes at off) taken at epoch t. */
    synchronized void fingerprint(double t, byte[] zones, int off) {
        try {
            String d = day.format(new Date((long) (t * 1000)));
            if (!d.equals(openDay)) {
                if (fpOut != null) {
                    fpOut.close();
                }
                fpOut = new FileOutputStream(new File(fpDir, d + ".bin"), true);
                openDay = d;
                prune();
            }
            rec.clear();
            rec.putDouble(t);
            rec.put(zones, off, 432);
            fpOut.write(rec.array());
        } catch (IOException e) {
            Log.w(TAG, "fingerprint log: " + e);
        }
    }

    /** Appends an event; fields is completed with t, ts and type. */
    synchronized void event(double t, String type, JSONObject fields) {
        try {
            JSONObject o = new JSONObject();
            o.put("t", Math.round(t * 1000) / 1000.0);
            o.put("ts", iso.format(new Date((long) (t * 1000))));
            o.put("type", type);
            if (fields != null) {
                java.util.Iterator<String> it = fields.keys();
                while (it.hasNext()) {
                    String k = it.next();
                    o.put(k, fields.get(k));
                }
            }
            FileOutputStream out = new FileOutputStream(events, true);
            out.write((o.toString() + "\n").getBytes("UTF-8"));
            out.close();
        } catch (Exception e) {
            Log.w(TAG, "event log: " + e);
        }
    }

    /** Fingerprint day files, oldest first. */
    File[] fpFiles() {
        File[] f = fpDir.listFiles();
        if (f == null) {
            return new File[0];
        }
        Arrays.sort(f);
        return f;
    }

    private void prune() {
        File[] f = fpFiles();
        String limit = day.format(new Date(System.currentTimeMillis() - FP_DAYS * 86400000L));
        long total = 0;
        for (File x : f) {
            total += x.length();
        }
        for (File x : f) {
            String name = x.getName();
            boolean old = name.compareTo(limit) < 0;
            if ((old || total > FP_MAX_BYTES) && !name.startsWith(openDay == null ? "-" : openDay)) {
                total -= x.length();
                if (!x.delete()) {
                    Log.w(TAG, "could not delete " + x);
                }
            }
        }
    }
}
