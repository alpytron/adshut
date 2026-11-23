// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.app;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.DashPathEffect;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Rect;
import android.graphics.RectF;
import android.media.Image;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowManager;

import org.json.JSONObject;

import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;

import adshut.core.Fingerprinter;
import adshut.core.Geometry;
import adshut.core.Setup;

/**
 * Launcher screen: camera setup and status.
 *
 * The whole screen shows a slowly alternating mid-grey checkerboard, so the TV screen is the only
 * thing changing in the camera's view and its outline can be measured every few seconds while the
 * camera is being aimed. The camera view, the measured outline, readouts and hints sit in a
 * central panel, away from the screen's edges, which must stay part of the pattern.
 *
 * Remote: OK saves the measured corners, LEFT/RIGHT switches between the camera view and the
 * straightened view (what the detector sees), BACK leaves. The service keeps running.
 */
public class MainActivity extends Activity implements CamService.SetupSink {
    static final long FLIP_MS = 1000;           // pattern change rate: slow, mid contrast
    static final int GREY_A = 0xff484848, GREY_B = 0xffb8b8b8;

    private SetupView view;
    private Fingerprinter loader;
    private final Setup setup = new Setup();
    private volatile Setup.Reading reading;
    private final List<double[][]> recent = new ArrayList<double[][]>();
    private volatile Bitmap preview, previewNext;
    private int[] argb;
    private volatile boolean straightened;
    private volatile String message = "";
    private volatile long messageUntil;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON | WindowManager.LayoutParams.FLAG_FULLSCREEN);
        view = new SetupView(this);
        setContentView(view);
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.CAMERA}, 1);
        }
        startService(new Intent(this, CamService.class));
    }

    @Override
    protected void onResume() {
        super.onResume();
        CamService.setupSink = this;
        view.setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
        view.invalidate();
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (CamService.setupSink == this) {
            CamService.setupSink = null;
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_DPAD_CENTER || keyCode == KeyEvent.KEYCODE_ENTER) {
            save();
            return true;
        }
        if (keyCode == KeyEvent.KEYCODE_DPAD_DOWN || keyCode == KeyEvent.KEYCODE_MENU) {
            startActivity(new Intent(this, SettingsActivity.class));
            return true;
        }
        if (keyCode == KeyEvent.KEYCODE_DPAD_LEFT || keyCode == KeyEvent.KEYCODE_DPAD_RIGHT) {
            straightened = !straightened;
            view.invalidate();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    private void save() {
        double[][] c;
        synchronized (recent) {
            c = recent.isEmpty() ? null : Setup.median(recent);
        }
        CamService s = CamService.instance();
        if (c == null || s == null) {
            say("Nothing to save yet: the screen has not been measured");
            return;
        }
        s.saveCorners(c);
        say("Corners saved");
    }

    private void say(String m) {
        message = m;
        messageUntil = SystemClock.uptimeMillis() + 4000;
        view.invalidate();
    }

    // ------------------------------------------------------------ camera frames (camera thread)

    @Override
    public void frame(Image img) {
        int w = img.getWidth() / 2, h = img.getHeight() / 2;
        if (loader == null || loader.width() != w || loader.height() != h) {
            loader = new Fingerprinter(w, h);
            argb = new int[w * h];
        }
        Image.Plane[] p = img.getPlanes();
        loader.load(bytes(p[0].getBuffer()), p[0].getRowStride(), bytes(p[1].getBuffer()), p[1].getRowStride(),
                p[1].getPixelStride(), bytes(p[2].getBuffer()), p[2].getRowStride(), p[2].getPixelStride());
        Setup.Reading r = setup.add(loader.luma(), w, h, SystemClock.uptimeMillis());
        if (r != null) {
            reading = r;
            synchronized (recent) {
                if (r.corners != null) {
                    recent.add(r.corners);
                    while (recent.size() > 3) {
                        recent.remove(0);
                    }
                } else {
                    recent.clear();
                }
            }
        }
        Setup.Reading cur = reading;
        if (straightened && cur != null && cur.corners != null) {
            straighten(cur.corners, w, h);
        } else {
            toArgb(w, h);
        }
        Bitmap b = previewNext;
        if (b == null || b.getWidth() != w || b.getHeight() != h) {
            b = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        }
        b.setPixels(argb, 0, w, 0, 0, w, h);
        previewNext = preview;
        preview = b;
        view.postInvalidate();
    }

    private byte[] scratchY, scratchU, scratchV;
    private int which;

    private byte[] bytes(ByteBuffer b) {
        b.rewind();
        int n = b.remaining();
        byte[] a;
        int k = which++ % 3;
        a = k == 0 ? scratchY : k == 1 ? scratchU : scratchV;
        if (a == null || a.length < n) {
            a = new byte[n];
            if (k == 0) {
                scratchY = a;
            } else if (k == 1) {
                scratchU = a;
            } else {
                scratchV = a;
            }
        }
        b.get(a, 0, n);
        return a;
    }

    private static int rgb(float y, float cb, float cr) {
        int r = clamp(y + 1.402f * (cr - 128));
        int g = clamp(y - 0.344136f * (cb - 128) - 0.714136f * (cr - 128));
        int b = clamp(y + 1.772f * (cb - 128));
        return 0xff000000 | (r << 16) | (g << 8) | b;
    }

    private static int clamp(float v) {
        return v < 0 ? 0 : v > 255 ? 255 : (int) v;
    }

    private void toArgb(int w, int h) {
        float[] y = loader.luma(), cb = loader.cb(), cr = loader.cr();
        for (int i = 0; i < w * h; i++) {
            argb[i] = rgb(y[i], cb[i], cr[i]);
        }
    }

    /** What the detector sees: the screen area warped to a rectangle (nearest neighbour). */
    private void straighten(double[][] c, int w, int h) {
        double[][] src = new double[4][];
        for (int i = 0; i < 4; i++) {
            src[i] = new double[] {c[i][0] * w, c[i][1] * h};
        }
        double[] m = Geometry.homography(new double[][] {{0, 0}, {w, 0}, {w, h}, {0, h}}, src);
        float[] y = loader.luma(), cb = loader.cb(), cr = loader.cr();
        for (int oy = 0; oy < h; oy++) {
            for (int ox = 0; ox < w; ox++) {
                double[] p = Geometry.apply(m, ox + 0.5, oy + 0.5);
                int x = (int) p[0], yy = (int) p[1];
                argb[oy * w + ox] = x < 0 || yy < 0 || x >= w || yy >= h ? 0xff000000
                        : rgb(y[yy * w + x], cb[yy * w + x], cr[yy * w + x]);
            }
        }
    }

    // ------------------------------------------------------------ drawing

    final class SetupView extends View {
        private final Paint fill = new Paint(), text = new Paint(Paint.ANTI_ALIAS_FLAG), line = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint dashed = new Paint(Paint.ANTI_ALIAS_FLAG);

        SetupView(Context c) {
            super(c);
            setFocusable(true);
            line.setStyle(Paint.Style.STROKE);
            line.setStrokeWidth(4);
            dashed.setStyle(Paint.Style.STROKE);
            dashed.setStrokeWidth(3);
            dashed.setColor(0xff4fa3ff);
            dashed.setPathEffect(new DashPathEffect(new float[] {14, 10}, 0));
        }

        @Override
        protected void onDraw(Canvas cv) {
            int W = getWidth(), H = getHeight();
            long now = SystemClock.uptimeMillis();
            // the measuring pattern over the whole screen
            boolean phase = (now / FLIP_MS) % 2 == 0;
            int cols = 32, rows = 18;
            for (int r = 0; r < rows; r++) {
                for (int c = 0; c < cols; c++) {
                    fill.setColor(((r + c) % 2 == 0) == phase ? GREY_A : GREY_B);
                    cv.drawRect(c * W / cols, r * H / rows, (c + 1) * W / cols, (r + 1) * H / rows, fill);
                }
            }
            // central panel, well inside the screen's edges
            RectF panel = new RectF(W * 0.2f, H * 0.17f, W * 0.8f, H * 0.83f);
            fill.setColor(0xf0181818);
            cv.drawRoundRect(panel, 16, 16, fill);
            float pad = H * 0.02f;
            float pw = panel.width() * 0.56f, ph = pw * 9 / 16;
            RectF pv = new RectF(panel.left + pad, panel.top + pad, panel.left + pad + pw, panel.top + pad + ph);
            Bitmap b = preview;
            if (b != null) {
                cv.drawBitmap(b, null, pv, null);
            } else {
                fill.setColor(0xff000000);
                cv.drawRect(pv, fill);
            }
            Setup.Reading r = reading;
            CamService s = CamService.instance();
            if (!straightened) {
                if (s != null) {
                    drawQuad(cv, pv, s.savedCorners(), dashed);
                }
                if (r != null && r.corners != null) {
                    line.setColor(r.hints.length == 1 && r.hints[0].startsWith("Good") ? 0xff3ddc84 : 0xffffc107);
                    drawQuad(cv, pv, r.corners, line);
                }
            }
            // text column
            float tx = pv.right + pad * 1.5f, ty = panel.top + pad + H * 0.03f, lh = H * 0.032f;
            text.setTextSize(H * 0.032f);
            text.setColor(Color.WHITE);
            cv.drawText("adshut · camera setup", tx, ty, text);
            text.setTextSize(H * 0.024f);
            text.setColor(0xffcfcfcf);
            ty += lh * 1.5f;
            if (r == null) {
                cv.drawText("Measuring…", tx, ty, text);
                ty += lh;
            } else if (r.corners == null) {
                cv.drawText("Screen not found", tx, ty, text);
                ty += lh;
            } else {
                cv.drawText(String.format("Screen width: %.0f%% of the picture", 100 * r.coverage), tx, ty, text);
                ty += lh;
                cv.drawText("Side angle: " + grade(r.sideRatio) + ",  vertical: " + grade(r.vertRatio), tx, ty, text);
                ty += lh;
                cv.drawText(String.format("Brightness %.0f,  overexposed %.0f%%", r.meanLuma, 100 * r.clipped), tx, ty, text);
                ty += lh;
            }
            if (s != null) {
                JSONObject st = s.liveStatus();
                ty += lh * 0.5f;
                cv.drawText("Mode " + st.optString("mode") + ",  templates " + st.optInt("templates")
                        + ",  " + st.optDouble("ms_per_frame") + " ms/frame", tx, ty, text);
                ty += lh;
                cv.drawText("Server: " + st.optString("sync"), tx, ty, text);
            }
            // hints and keys under the preview
            float hy = pv.bottom + pad + lh;
            text.setColor(0xffffd54f);
            text.setTextSize(H * 0.026f);
            if (r != null) {
                for (String hint : r.hints) {
                    cv.drawText(hint, pv.left, hy, text);
                    hy += lh * 1.1f;
                }
            }
            if (now < messageUntil) {
                text.setColor(0xff3ddc84);
                cv.drawText(message, pv.left, hy + lh * 0.3f, text);
            }
            text.setColor(0xff9e9e9e);
            text.setTextSize(H * 0.022f);
            cv.drawText((straightened ? "Showing what the detector sees. " : "Blue dashed: saved corners. ")
                            + "OK: save corners · ◀ ▶: camera / straightened view · ▼: settings · BACK: leave",
                    pv.left, panel.bottom - pad, text);
            postInvalidateDelayed(FLIP_MS - now % FLIP_MS + 5);   // next pattern change
        }

        private void drawQuad(Canvas cv, RectF pv, double[][] c, Paint p) {
            if (c == null) {
                return;
            }
            Path path = new Path();
            for (int i = 0; i <= 4; i++) {
                float x = pv.left + (float) c[i % 4][0] * pv.width(), y = pv.top + (float) c[i % 4][1] * pv.height();
                if (i == 0) {
                    path.moveTo(x, y);
                } else {
                    path.lineTo(x, y);
                }
            }
            cv.drawPath(path, p);
        }

        private String grade(double ratio) {
            return ratio >= 0.8 ? "good" : ratio >= 0.6 ? "moderate" : "steep";
        }
    }
}
