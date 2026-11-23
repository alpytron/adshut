// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

/** Four-point homographies and small linear algebra, as in recorder/screenfind.py. */
public final class Geometry {
    private Geometry() {
    }

    public static final double[][] UNIT_SQUARE = {{0, 0}, {1, 0}, {1, 1}, {0, 1}};

    /** 3x3 matrix (row-major, 9 values) taking the four src points onto the four dst points. */
    public static double[] homography(double[][] src, double[][] dst) {
        double[][] a = new double[8][9];
        for (int i = 0; i < 4; i++) {
            double x = src[i][0], y = src[i][1], u = dst[i][0], v = dst[i][1];
            a[2 * i] = new double[] {x, y, 1, 0, 0, 0, -u * x, -u * y, u};
            a[2 * i + 1] = new double[] {0, 0, 0, x, y, 1, -v * x, -v * y, v};
        }
        double[] h = solve(a, 8);
        return new double[] {h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], 1};
    }

    public static double[] apply(double[] m, double x, double y) {
        double w = m[6] * x + m[7] * y + m[8];
        return new double[] {(m[0] * x + m[1] * y + m[2]) / w, (m[3] * x + m[4] * y + m[5]) / w};
    }

    /** Gaussian elimination with partial pivoting on an n x (n+1) augmented matrix. */
    static double[] solve(double[][] a, int n) {
        for (int c = 0; c < n; c++) {
            int p = c;
            for (int r = c + 1; r < n; r++) {
                if (Math.abs(a[r][c]) > Math.abs(a[p][c])) {
                    p = r;
                }
            }
            if (Math.abs(a[p][c]) < 1e-12) {
                throw new ArithmeticException("singular");
            }
            double[] t = a[c];
            a[c] = a[p];
            a[p] = t;
            for (int r = 0; r < n; r++) {
                if (r == c) {
                    continue;
                }
                double f = a[r][c] / a[c][c];
                for (int k = c; k <= n; k++) {
                    a[r][k] -= f * a[c][k];
                }
            }
        }
        double[] x = new double[n];
        for (int i = 0; i < n; i++) {
            x[i] = a[i][n] / a[i][i];
        }
        return x;
    }

    /** Intersection of lines given as (point px,py; direction dx,dy). */
    static double[] intersect(double[] l1, double[] l2) {
        double det = l1[2] * -l2[3] - (-l2[2]) * l1[3];
        if (Math.abs(det) < 1e-12) {
            throw new ArithmeticException("parallel");
        }
        double bx = l2[0] - l1[0], by = l2[1] - l1[1];
        double t = (bx * -l2[3] - (-l2[2]) * by) / det;
        return new double[] {l1[0] + t * l1[2], l1[1] + t * l1[3]};
    }

    /** numpy.percentile with linear interpolation on a copy of the values. */
    static double percentile(float[] v, double q) {
        float[] s = v.clone();
        java.util.Arrays.sort(s);
        double pos = q / 100.0 * (s.length - 1);
        int lo = (int) Math.floor(pos);
        int hi = Math.min(lo + 1, s.length - 1);
        return s[lo] + (s[hi] - s[lo]) * (pos - lo);
    }

    /** numpy.median of doubles. */
    static double median(double[] v) {
        double[] s = v.clone();
        java.util.Arrays.sort(s);
        int n = s.length;
        return n % 2 == 1 ? s[n / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
    }
}
