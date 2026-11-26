// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.app;

import android.app.Notification;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.ImageFormat;
import android.graphics.Rect;
import android.graphics.YuvImage;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.util.Log;
import android.util.Range;
import android.util.Size;
import android.widget.Toast;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.ByteBuffer;
import java.util.Collections;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Foreground service: runs the live loop (Live: fingerprints, screen tracking, detector, mute)
 * while Live TV is in front, keeps the template set in sync with the template server, and
 * serves camera frames and the TV's state over HTTP. The camera is open while the live loop
 * wants frames or somebody is asking for them.
 *
 *   GET /config[?key=value&...]  show or change settings (server, tv, token, mode, toasts, corners)
 *   GET /live                    live loop status (JSON)
 *   GET /shot.jpg              one current frame   } only with "share the camera picture" on
 *   GET /stream.mjpg           multipart MJPEG stream } (TV settings screen), else 403
 *   GET /status                camera status (JSON)
 *   GET /state                 TV state: power, foreground app, mute, volume, channel (JSON)
 *   GET /events?since=N&wait=S state changes after event N, waiting up to S seconds for one
 *   GET /mute?on=1|0[&note=T]  mute or unmute the TV; a note is shown as a toast
 *   GET /toast?text=T          show a toast
 */
public class CamService extends Service {
    static final String TAG = "adshut";
    static final int PORT = 8765;
    static final int TARGET_W = 640, TARGET_H = 360;
    static final long FRAME_INTERVAL_MS = 200;   // 5 fps
    static final int JPEG_QUALITY = 70;
    static final long IDLE_CLOSE_MS = 15000;     // close the camera this long after the last request
    static final long RETRY_MS = 3000;           // wait this long before reopening after a failure
    static final long CAM_GRACE_MS = 30000;      // Live TV on this long without frames = camera problem
    static final long CAM_WARN_EVERY_MS = 5 * 60000;  // repeat the on-screen warning this often
    static final int MAX_CLIENTS = 6;

    private static final int IDLE = 0, OPENING = 1, RUNNING = 2;
    private static volatile CamService instance;

    /** Receives every camera frame while the setup screen is open (camera thread). */
    interface SetupSink {
        void frame(Image img);
    }

    static volatile SetupSink setupSink;

    static CamService instance() {
        return instance;
    }

    /** Corners from the setup screen: used from now on, and logged. */
    void saveCorners(double[][] c) {
        cfg.setCorners(c);
        live.reloadCorners();
        dlog.event(System.currentTimeMillis() / 1000.0, "calibration",
                Live.put(Live.put(new org.json.JSONObject(), "change", "setup"), "corners", Config.cornersJson(c)));
    }

    /** Settings changed in the settings screen: reach the server soon with them. */
    void settingsChanged() {
        sync.poke();
    }

    double[][] savedCorners() {
        return cfg.corners();
    }

    org.json.JSONObject liveStatus() {
        return live.status();
    }

    private HandlerThread camThread;
    private Handler camHandler;
    private CameraDevice device;
    private CameraCaptureSession session;
    private ImageReader reader;
    private volatile int state = IDLE;
    private volatile String lastError = "";
    private volatile String cameraInfo = "";
    private volatile long lastDemand, lastFailure, lastEncode, startedAt;
    private volatile long lastFrameAt;
    private volatile String cameraProblem = "";
    private long wantedSince, lastWarn;

    private final Object frameLock = new Object();
    private byte[] latestJpeg;
    private long frameSeq;
    private byte[] nv21;

    private ServerSocket server;
    private TvWatch watch;
    private Config cfg;
    private DataLog dlog;
    private Sync sync;
    private Live live;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final AtomicInteger clients = new AtomicInteger();
    private final AtomicInteger streamClients = new AtomicInteger();

    // ---------------------------------------------------------------- service lifecycle

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        startedAt = SystemClock.elapsedRealtime();
        Notification n = new Notification.Builder(this)
                .setContentTitle("AdShut")
                .setContentText("Serving camera frames on port " + PORT)
                .setSmallIcon(android.R.drawable.presence_video_online)
                .setOngoing(true)
                .build();
        startForeground(1, n);

        watch = new TvWatch(this);
        // The live loop works on the camera thread at background priority: the TV's own UI first.
        camThread = new HandlerThread("adshut-cam", Process.THREAD_PRIORITY_BACKGROUND);
        camThread.start();
        camHandler = new Handler(camThread.getLooper());

        cfg = new Config(this);
        dlog = new DataLog(new java.io.File(getFilesDir(), "data"));
        final Live[] ref = new Live[1];
        sync = new Sync(cfg, dlog, new java.io.File(getFilesDir(), "templates"), new Sync.Listener() {
            @Override
            public void templatesChanged() {
                if (ref[0] != null) {
                    ref[0].templatesChanged();
                }
            }
        });
        live = new Live(cfg, watch, dlog, sync, camHandler, new Live.Ui() {
            @Override
            public void toast(String text) {
                CamService.this.toast(text);
            }
        });
        ref[0] = live;
        watch.attach(dlog, live);
        watch.start();
        sync.start();
        camHandler.postDelayed(idleCheck, 1000);

        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                serve();
            }
        }, "adshut-http");
        t.setDaemon(true);
        t.start();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        instance = null;
        watch.stop();
        sync.stop();
        try {
            if (server != null) server.close();
        } catch (IOException ignored) {
        }
        camHandler.post(new Runnable() {
            @Override
            public void run() {
                stopCamera();
                camThread.quitSafely();
            }
        });
        super.onDestroy();
    }

    static String statusText() {
        CamService s = instance;
        return s == null ? "service not running" : "port " + PORT + "\n" + s.statusJson() + "\n" + s.live.status();
    }

    // ---------------------------------------------------------------- camera

    /** Called by HTTP threads whenever a frame is wanted. */
    private void demand() {
        lastDemand = SystemClock.elapsedRealtime();
        if (state == IDLE && lastDemand - lastFailure > RETRY_MS) {
            camHandler.post(new Runnable() {
                @Override
                public void run() {
                    startCamera();
                }
            });
        }
    }

    /** Every second: live loop housekeeping; camera open while the loop or a client wants frames. */
    private final Runnable idleCheck = new Runnable() {
        @Override
        public void run() {
            try {
                live.tick();
                long now = SystemClock.elapsedRealtime();
                boolean clientsWant = streamClients.get() > 0 || now - lastDemand <= IDLE_CLOSE_MS
                        || setupSink != null;
                boolean liveWants = live.wanted();
                checkCamera(liveWants, now);
                if (liveWants || setupSink != null) {
                    if (state == IDLE && now - lastFailure > RETRY_MS) {
                        startCamera();
                    }
                } else if (state != IDLE && !clientsWant) {
                    Log.i(TAG, "live loop paused (" + live.pausedBecause + ") and no clients: closing camera");
                    stopCamera();
                }
            } catch (Exception e) {
                Log.w(TAG, "housekeeping: " + e);
            }
            camHandler.postDelayed(this, 1000);
        }
    };

    /**
     * Warns on screen while Live TV is on but no frames arrive (camera unplugged, or the TV's USB
     * port switched to device mode because USB debugging was on at boot): without frames nothing
     * gets muted, and that is otherwise invisible. Logged as "camera" events (ok false/true).
     */
    private void checkCamera(boolean liveWants, long now) {
        if (!liveWants) {
            wantedSince = 0;
        } else if (wantedSince == 0) {
            wantedSince = now;
        }
        String problem = "";
        if (liveWants && now - wantedSince > CAM_GRACE_MS && now - lastFrameAt > CAM_GRACE_MS) {
            problem = state == RUNNING ? "no frames from the camera"
                    : lastError.isEmpty() ? "camera not running" : lastError;
        }
        if (!problem.isEmpty()) {
            if (cameraProblem.isEmpty()) {
                cameraEvent(false, problem);
            }
            if (lastWarn == 0 || now - lastWarn > CAM_WARN_EVERY_MS) {
                lastWarn = now;
                toast("AdShut: " + problem + " \u2013 ads are not muted.\n"
                        + "Check the webcam. If USB debugging is on, switch it off and restart the TV.");
            }
            cameraProblem = problem;
        } else if (!cameraProblem.isEmpty() && now - lastFrameAt < 5000) {
            cameraProblem = "";
            lastWarn = 0;
            cameraEvent(true, null);
            toast("AdShut: camera is back");
        }
    }

    private void cameraEvent(boolean ok, String error) {
        try {
            org.json.JSONObject o = new org.json.JSONObject();
            o.put("ok", ok);
            if (error != null) o.put("error", error);
            dlog.event(System.currentTimeMillis() / 1000.0, "camera", o);
        } catch (Exception e) {
            Log.w(TAG, "camera event: " + e);
        }
    }

    private void fail(String what, Throwable e) {
        lastError = what + (e == null ? "" : ": " + e);
        lastFailure = SystemClock.elapsedRealtime();
        Log.w(TAG, lastError);
        stopCamera();
    }

    private void startCamera() {
        if (state != IDLE) return;
        state = OPENING;
        try {
            CameraManager mgr = (CameraManager) getSystemService(Context.CAMERA_SERVICE);
            String[] ids = mgr.getCameraIdList();
            if (ids.length == 0) {
                fail("no camera attached", null);
                return;
            }
            String id = ids[0];
            CameraCharacteristics ch = mgr.getCameraCharacteristics(id);
            StreamConfigurationMap map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            Size size = pickSize(map.getOutputSizes(ImageFormat.YUV_420_888));
            final Range<Integer> fps = pickFps(ch.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES));
            cameraInfo = "id " + id + " " + size.getWidth() + "x" + size.getHeight() + " fps " + fps;
            Log.i(TAG, "opening camera " + cameraInfo);

            reader = ImageReader.newInstance(size.getWidth(), size.getHeight(), ImageFormat.YUV_420_888, 2);
            reader.setOnImageAvailableListener(onImage, camHandler);

            mgr.openCamera(id, new CameraDevice.StateCallback() {
                @Override
                public void onOpened(CameraDevice cam) {
                    device = cam;
                    try {
                        cam.createCaptureSession(Collections.singletonList(reader.getSurface()),
                                new CameraCaptureSession.StateCallback() {
                                    @Override
                                    public void onConfigured(CameraCaptureSession s) {
                                        session = s;
                                        try {
                                            CaptureRequest.Builder b =
                                                    device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                                            b.addTarget(reader.getSurface());
                                            if (fps != null) {
                                                b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fps);
                                            }
                                            s.setRepeatingRequest(b.build(), null, camHandler);
                                            state = RUNNING;
                                            lastError = "";
                                        } catch (Exception e) {
                                            fail("start capture", e);
                                        }
                                    }

                                    @Override
                                    public void onConfigureFailed(CameraCaptureSession s) {
                                        fail("session configuration failed", null);
                                    }
                                }, camHandler);
                    } catch (Exception e) {
                        fail("create session", e);
                    }
                }

                @Override
                public void onDisconnected(CameraDevice cam) {
                    device = cam;
                    fail("camera disconnected", null);
                }

                @Override
                public void onError(CameraDevice cam, int error) {
                    device = cam;
                    fail("camera error " + error, null);
                }
            }, camHandler);
        } catch (CameraAccessException e) {
            fail("camera access", e);
        } catch (SecurityException e) {
            fail("camera permission missing", e);
        } catch (RuntimeException e) {
            fail("camera setup", e);
        }
    }

    private void stopCamera() {
        try {
            if (session != null) session.close();
        } catch (Exception ignored) {
        }
        try {
            if (device != null) device.close();
        } catch (Exception ignored) {
        }
        try {
            if (reader != null) reader.close();
        } catch (Exception ignored) {
        }
        session = null;
        device = null;
        reader = null;
        state = IDLE;
    }

    /** Smallest area difference to the target, so we never pull more pixels than needed. */
    private static Size pickSize(Size[] sizes) {
        Size best = sizes[0];
        long bestScore = Long.MAX_VALUE;
        for (Size s : sizes) {
            long score = Math.abs((long) s.getWidth() * s.getHeight() - (long) TARGET_W * TARGET_H);
            if (s.getWidth() * 9 != s.getHeight() * 16) score += 20000; // prefer 16:9, like the screen
            if (score < bestScore) {
                bestScore = score;
                best = s;
            }
        }
        return best;
    }

    /** Lowest frame rate the camera offers; we only keep 5 fps anyway. */
    private static Range<Integer> pickFps(Range<Integer>[] ranges) {
        if (ranges == null || ranges.length == 0) return null;
        Range<Integer> best = ranges[0];
        for (Range<Integer> r : ranges) {
            if (r.getUpper() < best.getUpper()
                    || (r.getUpper().equals(best.getUpper()) && r.getLower() < best.getLower())) {
                best = r;
            }
        }
        return best;
    }

    private final ImageReader.OnImageAvailableListener onImage = new ImageReader.OnImageAvailableListener() {
        @Override
        public void onImageAvailable(ImageReader r) {
            Image img = null;
            try {
                img = r.acquireLatestImage();
                if (img == null) return;
                lastFrameAt = SystemClock.elapsedRealtime();
                live.onFrame(img);
                SetupSink sink = setupSink;
                if (sink != null) {
                    try {
                        sink.frame(img);
                    } catch (Exception e) {
                        Log.w(TAG, "setup frame: " + e);
                    }
                }
                long now = SystemClock.elapsedRealtime();
                if (streamClients.get() == 0 && now - lastDemand > IDLE_CLOSE_MS) return;  // nobody wants JPEGs
                if (now - lastEncode < FRAME_INTERVAL_MS - 10) return; // drop: over the frame budget
                lastEncode = now;
                int w = img.getWidth(), h = img.getHeight();
                if (nv21 == null || nv21.length != w * h * 3 / 2) nv21 = new byte[w * h * 3 / 2];
                toNv21(img, nv21);
                ByteArrayOutputStream out = new ByteArrayOutputStream(64 * 1024);
                new YuvImage(nv21, ImageFormat.NV21, w, h, null)
                        .compressToJpeg(new Rect(0, 0, w, h), JPEG_QUALITY, out);
                synchronized (frameLock) {
                    latestJpeg = out.toByteArray();
                    frameSeq++;
                    frameLock.notifyAll();
                }
            } catch (Exception e) {
                lastError = "frame: " + e;
                Log.w(TAG, lastError);
            } finally {
                if (img != null) img.close();
            }
        }
    };

    /** Copies a YUV_420_888 image into NV21, honouring row and pixel strides. */
    private static void toNv21(Image img, byte[] out) {
        int w = img.getWidth(), h = img.getHeight();
        Image.Plane[] p = img.getPlanes();

        ByteBuffer yb = p[0].getBuffer();
        int ys = p[0].getRowStride();
        for (int r = 0; r < h; r++) {
            yb.position(r * ys);
            yb.get(out, r * w, w);
        }

        ByteBuffer ub = p[1].getBuffer(), vb = p[2].getBuffer();
        int us = p[1].getRowStride(), up = p[1].getPixelStride();
        int vs = p[2].getRowStride(), vp = p[2].getPixelStride();
        int cw = w / 2, chh = h / 2;
        byte[] urow = new byte[(cw - 1) * up + 1];
        byte[] vrow = new byte[(cw - 1) * vp + 1];
        int o = w * h;
        for (int r = 0; r < chh; r++) {
            ub.position(r * us);
            ub.get(urow, 0, Math.min(urow.length, ub.remaining()));
            vb.position(r * vs);
            vb.get(vrow, 0, Math.min(vrow.length, vb.remaining()));
            for (int c = 0; c < cw; c++) {
                out[o++] = vrow[c * vp];
                out[o++] = urow[c * up];
            }
        }
    }

    // ---------------------------------------------------------------- http

    private void serve() {
        try {
            server = new ServerSocket(PORT);
            while (true) {
                final Socket s = server.accept();
                if (clients.get() >= MAX_CLIENTS) {
                    s.close();
                    continue;
                }
                Thread t = new Thread(new Runnable() {
                    @Override
                    public void run() {
                        clients.incrementAndGet();
                        try {
                            handle(s);
                        } catch (IOException ignored) {
                            // client went away
                        } finally {
                            clients.decrementAndGet();
                            try {
                                s.close();
                            } catch (IOException ignored) {
                            }
                        }
                    }
                }, "adshut-client");
                t.setDaemon(true);
                t.start();
            }
        } catch (IOException e) {
            if (instance != null) {
                lastError = "http server: " + e;
                Log.w(TAG, lastError);
            }
        }
    }

    private void handle(Socket s) throws IOException {
        s.setSoTimeout(10000);
        BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), "ISO-8859-1"));
        String line = in.readLine();
        if (line == null) return;
        String[] parts = line.split(" ");
        String path = parts.length > 1 ? parts[1] : "/";
        String query = "";
        int q = path.indexOf('?');
        if (q >= 0) {
            query = path.substring(q + 1);
            path = path.substring(0, q);
        }
        String h;
        while ((h = in.readLine()) != null && h.length() > 0) {
            // skip request headers
        }
        OutputStream out = s.getOutputStream();

        boolean frameRequest = path.equals("/shot.jpg") || path.equals("/stream.mjpg");
        if (frameRequest && !cfg.lanFrames()) {
            text(out, "403 Forbidden", "text/plain", "camera pictures stay on the TV; enable sharing on the "
                    + "TV in AdShut settings if a host computer needs them\n");
        } else if (path.equals("/shot.jpg")) {
            byte[] jpg = waitFrame(-1, 6000);
            if (jpg == null) {
                text(out, "503 Service Unavailable", "text/plain", "no frame: " + lastError + "\n");
            } else {
                head(out, "200 OK", "image/jpeg", jpg.length);
                out.write(jpg);
            }
        } else if (path.equals("/stream.mjpg")) {
            stream(out);
        } else if (path.equals("/status")) {
            text(out, "200 OK", "application/json", statusJson() + "\n");
        } else if (path.equals("/config")) {
            StringBuilder err = new StringBuilder();
            for (String kv : query.split("&")) {
                int eq = kv.indexOf('=');
                if (eq <= 0) {
                    continue;
                }
                String msg = cfg.set(URLDecoder.decode(kv.substring(0, eq), "UTF-8"),
                        URLDecoder.decode(kv.substring(eq + 1), "UTF-8"));
                if (msg != null) {
                    err.append(msg).append("; ");
                }
            }
            if (query.contains("corners=")) {
                live.reloadCorners();
            }
            if (query.contains("server=") || query.contains("token=")) {
                sync.poke();
            }
            text(out, err.length() > 0 ? "400 Bad Request" : "200 OK", "application/json",
                    (err.length() > 0 ? "{\"error\":\"" + esc(err.toString()) + "\"}" : cfg.toJson().toString()) + "\n");
        } else if (path.equals("/live")) {
            text(out, "200 OK", "application/json", live.status().toString() + "\n");
        } else if (path.equals("/toast")) {
            String msg = textParam(query, "text");
            if (msg != null) toast(msg);
            text(out, "200 OK", "text/plain", "ok\n");
        } else if (path.equals("/state") || path.equals("/events") || path.equals("/mute")) {
            try {
                String body;
                if (path.equals("/events")) {
                    long wait = Math.min(Math.max(param(query, "wait", 0), 0), 55);
                    s.setSoTimeout((int) (wait + 10) * 1000);
                    body = watch.eventsSince(param(query, "since", 0), wait * 1000).toString();
                } else {
                    if (path.equals("/mute")) {
                        watch.setMute(param(query, "on", 1) != 0);
                        String note = textParam(query, "note");
                        if (note != null) toast(note);
                        Thread.sleep(300);          // let the poll see the new state before answering
                    }
                    body = watch.state().toString();
                }
                text(out, "200 OK", "application/json", body + "\n");
            } catch (Exception e) {
                text(out, "500 Internal Server Error", "text/plain", e + "\n");
            }
        } else if (path.equals("/")) {
            text(out, "200 OK", "text/html",
                    "<html><body style='background:#111;color:#ccc;font-family:sans-serif'>"
                            + "<h3>AdShut</h3>" + (cfg.lanFrames() ? "<img src='/stream.mjpg'>" : "<p>camera pictures stay on the TV</p>")
                            + "<p><a href='/status'>status</a></p></body></html>");
        } else {
            text(out, "404 Not Found", "text/plain", "not found\n");
        }
        out.flush();
    }

    private void stream(OutputStream out) throws IOException {
        streamClients.incrementAndGet();
        try {
            out.write(("HTTP/1.1 200 OK\r\nConnection: close\r\nCache-Control: no-store\r\n"
                    + "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n").getBytes("ISO-8859-1"));
            long seq = -1;
            long lastSent = SystemClock.elapsedRealtime();
            while (true) {
                byte[] jpg;
                synchronized (frameLock) {
                    demand();
                    if (frameSeq == seq || latestJpeg == null) {
                        try {
                            frameLock.wait(2000);
                        } catch (InterruptedException e) {
                            return;
                        }
                    }
                    if (frameSeq == seq || latestJpeg == null) {
                        if (SystemClock.elapsedRealtime() - lastSent > 20000) return; // camera is gone
                        continue;
                    }
                    seq = frameSeq;
                    jpg = latestJpeg;
                }
                out.write(("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + jpg.length + "\r\n\r\n")
                        .getBytes("ISO-8859-1"));
                out.write(jpg);
                out.write("\r\n".getBytes("ISO-8859-1"));
                out.flush();
                lastSent = SystemClock.elapsedRealtime();
            }
        } finally {
            streamClients.decrementAndGet();
        }
    }

    /** Waits for a frame newer than {@code afterSeq}; -1 means newer than now. */
    private byte[] waitFrame(long afterSeq, long timeoutMs) {
        long deadline = SystemClock.elapsedRealtime() + timeoutMs;
        synchronized (frameLock) {
            // A frame from a running camera is at most one interval old: good enough for a snapshot.
            if (afterSeq < 0) afterSeq = state == RUNNING && latestJpeg != null ? frameSeq - 1 : frameSeq;
            while (frameSeq <= afterSeq) {
                demand();
                long left = deadline - SystemClock.elapsedRealtime();
                if (left <= 0) return null;
                try {
                    frameLock.wait(Math.min(left, 1000));
                } catch (InterruptedException e) {
                    return null;
                }
            }
            demand();
            return latestJpeg;
        }
    }

    private String statusJson() {
        String st = state == RUNNING ? "running" : state == OPENING ? "opening" : "idle";
        long frames;
        synchronized (frameLock) {
            frames = frameSeq;
        }
        return "{\"camera\":\"" + st + "\",\"info\":\"" + esc(cameraInfo) + "\",\"frames\":" + frames
                + ",\"clients\":" + clients.get() + ",\"stream_clients\":" + streamClients.get()
                + ",\"uptime_s\":" + (SystemClock.elapsedRealtime() - startedAt) / 1000
                + ",\"last_error\":\"" + esc(lastError) + "\",\"camera_problem\":\"" + esc(cameraProblem) + "\"}";
    }

    /** Shows a short toast over whatever is on screen (text toasts are allowed from the background). */
    private void toast(final String msg) {
        main.post(new Runnable() {
            @Override
            public void run() {
                Toast.makeText(CamService.this, msg, Toast.LENGTH_LONG).show();
            }
        });
    }

    /** URL-encoded text query parameter, or null when absent. */
    private static String textParam(String query, String name) {
        for (String kv : query.split("&")) {
            if (kv.startsWith(name + "=")) {
                try {
                    String v = URLDecoder.decode(kv.substring(name.length() + 1), "UTF-8");
                    return v.length() > 80 ? v.substring(0, 80) : v;
                } catch (Exception e) {
                    return null;
                }
            }
        }
        return null;
    }

    /** Numeric query parameter, or {@code dflt} when absent or malformed. */
    private static long param(String query, String name, long dflt) {
        for (String kv : query.split("&")) {
            if (kv.startsWith(name + "=")) {
                try {
                    return Long.parseLong(kv.substring(name.length() + 1));
                } catch (NumberFormatException e) {
                    return dflt;
                }
            }
        }
        return dflt;
    }

    private static String esc(String s) {
        return s.replace("\\", "\\\\").replace("\"", "'").replace("\n", " ");
    }

    private static void head(OutputStream out, String status, String type, int len) throws IOException {
        out.write(("HTTP/1.1 " + status + "\r\nConnection: close\r\nCache-Control: no-store\r\nContent-Type: "
                + type + "\r\nContent-Length: " + len + "\r\n\r\n").getBytes("ISO-8859-1"));
    }

    private static void text(OutputStream out, String status, String type, String body) throws IOException {
        byte[] b = body.getBytes("UTF-8");
        head(out, status, type, b.length);
        out.write(b);
    }
}
