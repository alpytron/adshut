// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.app;

import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import adshut.core.Template;

/**
 * Talks to the template server (server/server.py): keeps the local copy of the template library
 * current and uploads this TV's fingerprints and events, resuming where the server left off.
 * Runs every PERIOD_MS, or sooner when asked (channel change).
 */
final class Sync {
    static final String TAG = "adshut";
    static final long PERIOD_MS = 60000;
    static final int CHUNK = 2 * 1024 * 1024;

    interface Listener {
        /** A new template set is available. */
        void templatesChanged();
    }

    private final Config cfg;
    private final DataLog log;
    private final File tplDir;
    private final Listener listener;
    private volatile boolean running = true;
    private final Object wake = new Object();
    private Map<String, List<Template>> byChannel = new HashMap<String, List<Template>>();
    private String version = "";
    volatile String lastResult = "not run yet";
    volatile long lastOk;

    Sync(Config cfg, DataLog log, File tplDir, Listener listener) {
        this.cfg = cfg;
        this.log = log;
        this.tplDir = tplDir;
        this.listener = listener;
        tplDir.mkdirs();
        loadCached();
    }

    void start() {
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                loop();
            }
        }, "adshut-sync");
        t.setDaemon(true);
        t.setPriority(Thread.MIN_PRIORITY);
        t.start();
    }

    void stop() {
        running = false;
        poke();
    }

    /** Run soon (e.g. the channel changed). */
    void poke() {
        synchronized (wake) {
            wake.notifyAll();
        }
    }

    synchronized List<Template> templates(String channel) {
        List<Template> l = channel == null ? null : byChannel.get(channel);
        return l == null ? new ArrayList<Template>() : l;
    }

    synchronized String version() {
        return version;
    }

    private void loop() {
        while (running) {
            if (cfg.server().length() > 0 && cfg.token().length() > 0) {
                try {
                    boolean changed = templatesUpdate();
                    String up = upload();
                    lastResult = (changed ? "templates updated; " : "") + up;
                    lastOk = System.currentTimeMillis();
                    if (changed) {
                        listener.templatesChanged();
                    }
                } catch (Exception e) {
                    lastResult = "failed: " + e;
                    Log.w(TAG, "sync: " + e);
                }
            } else {
                lastResult = "no server configured";
            }
            synchronized (wake) {
                try {
                    wake.wait(PERIOD_MS);
                } catch (InterruptedException e) {
                    return;
                }
            }
        }
    }

    // ------------------------------------------------------------ templates

    private boolean templatesUpdate() throws Exception {
        HttpURLConnection c = open("/v1/templates", "GET");
        if (version.length() > 0) {
            c.setRequestProperty("If-None-Match", version);
        }
        int code = c.getResponseCode();
        if (code == 304) {
            c.disconnect();
            return false;
        }
        if (code != 200) {
            throw new IOException("templates: HTTP " + code);
        }
        String body = new String(readAll(c.getInputStream()), "UTF-8");
        c.disconnect();
        JSONObject idx = new JSONObject(body);
        JSONArray list = idx.getJSONArray("templates");
        Set<String> keep = new HashSet<String>();
        for (int i = 0; i < list.length(); i++) {
            String name = list.getJSONObject(i).getString("name");
            keep.add(name + ".zones");
            File f = new File(tplDir, name + ".zones");
            if (!f.exists()) {
                HttpURLConnection z = open("/v1/templates/" + URLEncoder.encode(name, "UTF-8") + ".zones", "GET");
                if (z.getResponseCode() != 200) {
                    throw new IOException("template " + name + ": HTTP " + z.getResponseCode());
                }
                byte[] data = readAll(z.getInputStream());
                z.disconnect();
                File tmp = new File(tplDir, name + ".tmp");
                FileOutputStream out = new FileOutputStream(tmp);
                out.write(data);
                out.close();
                if (!tmp.renameTo(f)) {
                    throw new IOException("cannot store " + f);
                }
            }
        }
        FileOutputStream out = new FileOutputStream(new File(tplDir, "index.json"));
        out.write(body.getBytes("UTF-8"));
        out.close();
        File[] old = tplDir.listFiles();
        if (old != null) {
            for (File f : old) {
                if (f.getName().endsWith(".zones") && !keep.contains(f.getName())) {
                    f.delete();
                }
            }
        }
        return loadCached();
    }

    /** Builds the in-memory set from the cached index and zones; true if it loaded. */
    private boolean loadCached() {
        try {
            File idxFile = new File(tplDir, "index.json");
            if (!idxFile.exists()) {
                return false;
            }
            JSONObject idx = new JSONObject(new String(readAll(new FileInputStream(idxFile)), "UTF-8"));
            JSONArray list = idx.getJSONArray("templates");
            Map<String, List<Template>> map = new HashMap<String, List<Template>>();
            for (int i = 0; i < list.length(); i++) {
                JSONObject e = list.getJSONObject(i);
                File f = new File(tplDir, e.getString("name") + ".zones");
                if (!f.exists()) {
                    continue;
                }
                Template t = new Template(e.getString("name"), e.getString("kind"),
                        (float) e.optDouble("threshold", 0.8), readAll(new FileInputStream(f)));
                String ch = e.getString("channel");
                if (!map.containsKey(ch)) {
                    map.put(ch, new ArrayList<Template>());
                }
                map.get(ch).add(t);
            }
            synchronized (this) {
                byChannel = map;
                version = idx.optString("version", "");
            }
            return true;
        } catch (Exception e) {
            Log.w(TAG, "template cache: " + e);
            return false;
        }
    }

    // ------------------------------------------------------------ uploads

    private String upload() throws Exception {
        String tv = URLEncoder.encode(cfg.tv(), "UTF-8");
        HttpURLConnection c = open("/v1/sync/" + tv, "GET");
        if (c.getResponseCode() != 200) {
            throw new IOException("sync status: HTTP " + c.getResponseCode());
        }
        JSONObject st = new JSONObject(new String(readAll(c.getInputStream()), "UTF-8"));
        c.disconnect();
        JSONObject remoteFp = st.optJSONObject("fp");
        long sent = 0;
        for (File f : log.fpFiles()) {
            String day = f.getName().replace(".bin", "");
            long have = remoteFp == null ? 0 : remoteFp.optLong(day, 0);
            long size = f.length() - f.length() % DataLog.RECORD;     // whole records only
            sent += push(f, have, size, "/v1/sync/" + tv + "/fp/" + day);
        }
        long evHave = st.optLong("events", 0);
        if (log.events.exists()) {
            sent += push(log.events, evHave, log.events.length(), "/v1/sync/" + tv + "/events");
        }
        return "uploaded " + sent + " bytes";
    }

    private long push(File f, long from, long to, String path) throws Exception {
        long sent = 0;
        RandomAccessFile raf = new RandomAccessFile(f, "r");
        try {
            while (from < to) {
                int n = (int) Math.min(CHUNK, to - from);
                if (path.contains("/fp/")) {
                    n -= n % DataLog.RECORD;
                }
                byte[] buf = new byte[n];
                raf.seek(from);
                raf.readFully(buf);
                HttpURLConnection c = open(path + "?offset=" + from, "PUT");
                c.setDoOutput(true);
                c.setFixedLengthStreamingMode(n);
                OutputStream out = c.getOutputStream();
                out.write(buf);
                out.close();
                int code = c.getResponseCode();
                c.disconnect();
                if (code != 200) {
                    throw new IOException(path + ": HTTP " + code);
                }
                from += n;
                sent += n;
            }
        } finally {
            raf.close();
        }
        return sent;
    }

    // ------------------------------------------------------------ http

    private HttpURLConnection open(String path, String method) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(cfg.server() + path).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(5000);
        c.setReadTimeout(20000);
        c.setRequestProperty("Authorization", "Bearer " + cfg.token());
        return c;
    }

    static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] b = new byte[16384];
        int n;
        while ((n = in.read(b)) > 0) {
            out.write(b, 0, n);
        }
        in.close();
        return out.toByteArray();
    }
}
