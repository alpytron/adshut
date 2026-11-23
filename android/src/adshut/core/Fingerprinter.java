// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

/**
 * Camera frame -> 16x9 zone fingerprint, replacing the recorder's JPEG path (decode at half size,
 * perspective warp of the screen area to RECT_W x RECT_H, zone means).
 *
 * Input is the camera's YUV 4:2:0 frame (640x360): the luma is halved by 2x2 averaging to the
 * 320x180 plane the recorder's reduced-size JPEG decode gave, the chroma planes are already that
 * size. The warp follows Pillow's PERSPECTIVE/BILINEAR conventions (output pixel centres, input
 * sampled at centre - 0.5, clipped edges, truncation to 8 bits), so fingerprints and templates
 * from the recorder stay comparable.
 */
public final class Fingerprinter {
    public static final int RECT_W = 320, RECT_H = 180, GRID_W = 16, GRID_H = 9;
    static final int CELL_W = RECT_W / GRID_W, CELL_H = RECT_H / GRID_H;

    private final int w, h;                 // half-size planes (320 x 180 for a 640 x 360 camera)
    private final float[] y, cb, cr;        // planes the warp samples
    private final float[] luma;             // = y, for the screen tracker
    private double[] persp;                 // output (x, y) -> input (u, v), from the corners
    private final int[] sums = new int[GRID_W * GRID_H * 3];

    public Fingerprinter(int halfW, int halfH) {
        w = halfW;
        h = halfH;
        y = new float[w * h];
        cb = new float[w * h];
        cr = new float[w * h];
        luma = y;
    }

    /** Corners TL, TR, BR, BL as fractions of the camera frame. */
    public void setCorners(double[][] corners) {
        double[][] src = new double[4][];
        for (int i = 0; i < 4; i++) {
            src[i] = new double[] {corners[i][0] * w, corners[i][1] * h};
        }
        double[][] dst = {{0, 0}, {RECT_W, 0}, {RECT_W, RECT_H}, {0, RECT_H}};
        persp = Geometry.homography(dst, src);
    }

    /** Loads a frame from YUV planes (any row/pixel strides; chroma at half resolution). */
    public void load(byte[] yp, int yRow, byte[] up, int uRow, int uPix, byte[] vp, int vRow, int vPix) {
        for (int r = 0; r < h; r++) {
            int a = 2 * r * yRow, b = a + yRow;
            for (int c = 0; c < w; c++) {
                int s = (yp[a + 2 * c] & 0xff) + (yp[a + 2 * c + 1] & 0xff) + (yp[b + 2 * c] & 0xff)
                        + (yp[b + 2 * c + 1] & 0xff);
                y[r * w + c] = (s + 2) >> 2;
                cb[r * w + c] = up[r * uRow + c * uPix] & 0xff;
                cr[r * w + c] = vp[r * vRow + c * vPix] & 0xff;
            }
        }
    }

    /** Loads already half-size planes (tests, or a camera that delivers them directly). */
    public void loadHalf(byte[] yh, byte[] uh, byte[] vh) {
        for (int i = 0; i < w * h; i++) {
            y[i] = yh[i] & 0xff;
            cb[i] = uh[i] & 0xff;
            cr[i] = vh[i] & 0xff;
        }
    }

    /** Chroma planes of the loaded frame at half size (Cb, Cr), e.g. for a preview. */
    public float[] cb() {
        return cb;
    }

    public float[] cr() {
        return cr;
    }

    /** Luma of the loaded frame at half size (w x h), for ScreenTracker.add. */
    public float[] luma() {
        return luma;
    }

    public int width() {
        return w;
    }

    public int height() {
        return h;
    }

    /** Fingerprint of the loaded frame into out[off .. off+432): 9 rows x 16 zones x (Y, Cb, Cr). */
    public void fingerprint(byte[] out, int off) {
        java.util.Arrays.fill(sums, 0);
        double[] m = persp;
        for (int oy = 0; oy < RECT_H; oy++) {
            double yy = oy + 0.5;
            for (int ox = 0; ox < RECT_W; ox++) {
                double xx = ox + 0.5;
                double d = m[6] * xx + m[7] * yy + m[8];
                double xin = (m[0] * xx + m[1] * yy + m[2]) / d;
                double yin = (m[3] * xx + m[4] * yy + m[5]) / d;
                int cell = ((oy / CELL_H) * GRID_W + ox / CELL_W) * 3;
                if (xin < 0 || xin >= w || yin < 0 || yin >= h) {
                    continue;                               // outside the frame: black, as in Pillow
                }
                xin -= 0.5;
                yin -= 0.5;
                int x0 = (int) Math.floor(xin), y0 = (int) Math.floor(yin);
                double dx = xin - x0, dy = yin - y0;
                int xa = clip(x0, w), xb = clip(x0 + 1, w), ya = clip(y0, h);
                boolean hasB = y0 + 1 >= 0 && y0 + 1 < h;
                int yb = hasB ? y0 + 1 : ya;
                sums[cell] += sample(y, xa, xb, ya, yb, dx, dy, hasB);
                sums[cell + 1] += sample(cb, xa, xb, ya, yb, dx, dy, hasB);
                sums[cell + 2] += sample(cr, xa, xb, ya, yb, dx, dy, hasB);
            }
        }
        int n = CELL_W * CELL_H;
        for (int i = 0; i < sums.length; i++) {
            out[off + i] = (byte) Math.rint((double) sums[i] / n);
        }
    }

    private int sample(float[] p, int xa, int xb, int ya, int yb, double dx, double dy, boolean hasB) {
        double v1 = p[ya * w + xa] + (p[ya * w + xb] - p[ya * w + xa]) * dx;
        double v2 = hasB ? p[yb * w + xa] + (p[yb * w + xb] - p[yb * w + xa]) * dx : v1;
        return (int) (v1 + (v2 - v1) * dy);                    // Pillow truncates to 8 bits
    }

    private static int clip(int v, int size) {
        return v < 0 ? 0 : (v >= size ? size - 1 : v);
    }
}
