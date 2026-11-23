// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

/**
 * Camera-invariant frame features from a zone fingerprint; a Java port of detector/fp.py.
 *
 * A fingerprint is 9x16 zones x (Y, Cb, Cr) zone means, 432 unsigned bytes in row-major order.
 * luma: zone Y minus the frame mean, over the frame's Y spread (floored at MIN_SPREAD);
 * chroma: zone Cb/Cr minus the frame mean, on a fixed scale. The vector is L2-normalised, so
 * two frames match when their features correlate. The weight (0..1) says how much structure the
 * frame has: black or single-colour frames all look alike and should count little.
 */
public final class Features {
    public static final int ZONES = 9 * 16;
    public static final int BYTES = ZONES * 3;
    public static final int DIM = ZONES * 3;
    static final float CHROMA_SCALE = 1f / 12f;
    static final float MIN_SPREAD = 6f;

    private Features() {
    }

    /** Fills out[DIM] with the features of zones[off..off+BYTES) and returns the frame weight. */
    public static float compute(byte[] zones, int off, float[] out) {
        double ys = 0, cbs = 0, crs = 0;
        for (int z = 0; z < ZONES; z++) {
            ys += zones[off + 3 * z] & 0xff;
            cbs += zones[off + 3 * z + 1] & 0xff;
            crs += zones[off + 3 * z + 2] & 0xff;
        }
        float ym = (float) (ys / ZONES), cbm = (float) (cbs / ZONES), crm = (float) (crs / ZONES);
        double var = 0;
        for (int z = 0; z < ZONES; z++) {
            float d = (zones[off + 3 * z] & 0xff) - ym;
            var += d * d;
        }
        float spread = (float) Math.sqrt(var / ZONES);
        float div = Math.max(spread, MIN_SPREAD);
        double norm = 0;
        for (int z = 0; z < ZONES; z++) {
            float l = ((zones[off + 3 * z] & 0xff) - ym) / div;
            float cb = ((zones[off + 3 * z + 1] & 0xff) - cbm) * CHROMA_SCALE;
            float cr = ((zones[off + 3 * z + 2] & 0xff) - crm) * CHROMA_SCALE;
            out[z] = l;
            out[ZONES + 2 * z] = cb;
            out[ZONES + 2 * z + 1] = cr;
            norm += l * l + cb * cb + cr * cr;
        }
        float inv = (float) (1.0 / Math.max(Math.sqrt(norm), 1e-6));
        for (int i = 0; i < DIM; i++) {
            out[i] *= inv;
        }
        return Math.min(1f, Math.max(0f, spread / (2 * MIN_SPREAD)));
    }

    public static float dot(float[] a, int aoff, float[] b, int boff) {
        float s = 0;
        for (int i = 0; i < DIM; i++) {
            s += a[aoff + i] * b[boff + i];
        }
        return s;
    }
}
