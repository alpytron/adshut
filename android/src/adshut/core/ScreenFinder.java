// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

import java.util.ArrayList;
import java.util.List;

/**
 * Finds the TV screen in the camera frame from how much fine detail changes over time; a Java
 * port of recorder/screenfind.py (activity map, threshold, blob, Hough lines, least-squares
 * polish, inset), checked against it by TrackerMain on recorded frames.
 */
public final class ScreenFinder {
    static final int HIGHPASS_RADIUS = 2;
    static final double EDGE_INSET_PER_320 = 2.0;

    private ScreenFinder() {
    }

    /** Thrown when the activity map has no usable screen outline; the message says why. */
    public static final class NoScreen extends Exception {
        NoScreen(String why) {
            super(why);
        }
    }

    /** Result of a measurement: corners TL, TR, BR, BL in map pixels, plus fit details. */
    public static final class Found {
        public final double[][] corners;
        public final double area, inside, outside, edgeRms;

        Found(double[][] corners, double area, double inside, double outside, double edgeRms) {
            this.corners = corners;
            this.area = area;
            this.inside = inside;
            this.outside = outside;
            this.edgeRms = edgeRms;
        }
    }

    /** Mean over a (2r+1)^2 window with edge padding (screenfind._box). */
    static float[] box(float[] a, int w, int h, int r) {
        int pw = w + 2 * r, ph = h + 2 * r;
        double[] c = new double[(pw + 1) * (ph + 1)];
        for (int y = 0; y < ph; y++) {
            int sy = Math.min(Math.max(y - r, 0), h - 1);
            double row = 0;
            for (int x = 0; x < pw; x++) {
                int sx = Math.min(Math.max(x - r, 0), w - 1);
                row += a[sy * w + sx];
                c[(y + 1) * (pw + 1) + x + 1] = c[y * (pw + 1) + x + 1] + row;
            }
        }
        int k = 2 * r + 1;
        float[] out = new float[w * h];
        for (int y = 0; y < h; y++) {
            for (int x = 0; x < w; x++) {
                double s = c[(y + k) * (pw + 1) + x + k] - c[y * (pw + 1) + x + k]
                        - c[(y + k) * (pw + 1) + x] + c[y * (pw + 1) + x];
                out[y * w + x] = (float) (s / (k * k));
            }
        }
        return out;
    }

    /** Accumulates frame-to-frame change of fine detail (screenfind.Activity). */
    public static final class Activity {
        private float[] prev, total;
        private int count, w, h;

        public void add(float[] luma, int w, int h) {
            float[] b = box(luma, w, h, HIGHPASS_RADIUS);
            float[] hp = new float[w * h];
            for (int i = 0; i < hp.length; i++) {
                hp[i] = luma[i] - b[i];
            }
            if (prev != null && this.w == w && this.h == h) {
                if (total == null) {
                    total = new float[w * h];
                }
                for (int i = 0; i < hp.length; i++) {
                    total[i] += Math.abs(hp[i] - prev[i]);
                }
                count++;
            }
            prev = hp;
            this.w = w;
            this.h = h;
        }

        /** Mean activity map since the last take(), or null if no frame pairs were added. */
        public float[] take() {
            if (count == 0) {
                return null;
            }
            float[] out = total;
            for (int i = 0; i < out.length; i++) {
                out[i] /= count;
            }
            total = null;
            count = 0;
            return out;
        }

        public int width() {
            return w;
        }

        public int height() {
            return h;
        }
    }

    /** Corners TL, TR, BR, BL in pixels of the w x h activity map (screenfind.find_screen). */
    public static Found find(float[] act, int w, int h) throws NoScreen {
        int n = w * h;
        double lo = Geometry.percentile(act, 25), hi = Geometry.percentile(act, 90);
        double thr = lo + 0.25 * (hi - lo);
        float[] above = new float[n];
        for (int i = 0; i < n; i++) {
            above[i] = act[i] > thr ? 1f : 0f;
        }
        float[] smooth = box(above, w, h, 1);
        boolean[] mask = new boolean[n];
        for (int i = 0; i < n; i++) {
            mask[i] = smooth[i] > 0.5f;
        }
        boolean[] blob = blob(mask, w, h);
        int count = 0;
        double inS = 0, outS = 0;
        for (int i = 0; i < n; i++) {
            if (blob[i]) {
                count++;
                inS += act[i];
            } else {
                outS += act[i];
            }
        }
        double frac = (double) count / n;
        if (!(frac > 0.04 && frac < 0.9)) {
            throw new NoScreen(String.format("active area is %.0f%% of the frame", 100 * frac));
        }
        double inside = inS / count, outside = outS / (n - count);
        if (inside < 2.0 || inside < 2.5 * outside) {
            throw new NoScreen(String.format("too little motion on screen (%.1f inside, %.1f outside)", inside, outside));
        }
        boolean[] solid = fillHoles(blob, w, h);              // outer outline only
        // outline pixels (array border pixels of the region count as outline, as in numpy)
        List<double[]> pts = new ArrayList<double[]>();
        List<double[]> body = new ArrayList<double[]>();
        int bodyIdx = 0;
        for (int y = 0; y < h; y++) {
            for (int x = 0; x < w; x++) {
                int i = y * w + x;
                if (!solid[i]) {
                    continue;
                }
                if (bodyIdx++ % 7 == 0) {
                    body.add(new double[] {x, y});
                }
                boolean interior = x > 0 && y > 0 && x < w - 1 && y < h - 1
                        && solid[i - w] && solid[i + w] && solid[i - 1] && solid[i + 1];
                if (!interior) {
                    pts.add(new double[] {x, y});
                }
            }
        }
        double cx = 0, cy = 0;
        for (double[] p : pts) {
            cx += p[0];
            cy += p[1];
        }
        cx /= pts.size();
        cy /= pts.size();
        List<double[]> rough = new ArrayList<double[]>();         // theta, rho, votes
        for (double[] l : hough(pts, w, h, 8)) {
            double nx = Math.cos(l[0]), ny = Math.sin(l[0]);
            double sgn = Math.signum(cx * nx + cy * ny - l[1]);
            int ok = 0;
            for (double[] b : body) {
                if ((b[0] * nx + b[1] * ny - l[1]) * sgn > -2.5) {
                    ok++;
                }
            }
            if (l[2] >= 15 && (double) ok / body.size() > 0.97) {
                rough.add(l);
            }
        }
        if (rough.size() < 4) {
            throw new NoScreen("outline does not have four straight edges");
        }
        rough = rough.subList(0, 4);
        // edges in order around the blob; corners where neighbours cross
        double[][] sides = new double[4][];
        double[] ang = new double[4];
        for (int i = 0; i < 4; i++) {
            double[] l = rough.get(i);
            double nx = Math.cos(l[0]), ny = Math.sin(l[0]);
            double d = cx * nx + cy * ny - l[1];
            double fx = cx - d * nx, fy = cy - d * ny;
            ang[i] = Math.atan2(fy - cy, fx - cx);
            sides[i] = new double[] {fx, fy, -ny, nx};
        }
        sides = sortBy(sides, ang);
        double[][] quad = new double[4][];
        try {
            for (int i = 0; i < 4; i++) {
                quad[i] = Geometry.intersect(sides[(i + 3) % 4], sides[i]);
            }
        } catch (ArithmeticException e) {
            throw new NoScreen("outline edges are parallel");
        }
        double[][] lines = new double[4][];
        double maxRes = 0;
        double qx = 0, qy = 0;
        for (double[] q : quad) {
            qx += q[0] / 4;
            qy += q[1] / 4;
        }
        for (int i = 0; i < 4; i++) {
            double[] a = quad[i], b = quad[(i + 1) % 4];
            double len = Math.hypot(b[0] - a[0], b[1] - a[1]);
            double[] line = {(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (b[0] - a[0]) / len, (b[1] - a[1]) / len};
            List<double[]> sel = null;
            double[] dist = null;
            for (double tol : new double[] {3.0, 2.0, 1.5}) {
                sel = new ArrayList<double[]>();
                List<Double> ds = new ArrayList<Double>();
                for (double[] p : pts) {
                    double rx = p[0] - line[0], ry = p[1] - line[1];
                    double along = rx * line[2] + ry * line[3];
                    double dd = Math.abs(rx * -line[3] + ry * line[2]);
                    if (dd < tol && Math.abs(along) < 0.42 * len) {
                        sel.add(p);
                        ds.add(dd);
                    }
                }
                if (sel.size() < 8) {
                    throw new NoScreen("an edge of the outline is not straight enough");
                }
                dist = new double[ds.size()];
                for (int k = 0; k < dist.length; k++) {
                    dist[k] = ds.get(k);
                }
                line = fitLine(sel);
            }
            if (sel.size() < 0.15 * len) {
                throw new NoScreen("an edge of the outline is not straight enough");
            }
            double ss = 0;
            for (double d : dist) {
                ss += d * d;
            }
            maxRes = Math.max(maxRes, Math.sqrt(ss / dist.length));
            double nx = -line[3], ny = line[2];                 // move inwards to undo the filter smear
            if ((qx - line[0]) * nx + (qy - line[1]) * ny < 0) {
                nx = -nx;
                ny = -ny;
            }
            double inset = EDGE_INSET_PER_320 * w / 320.0;
            lines[i] = new double[] {line[0] + nx * inset, line[1] + ny * inset, line[2], line[3]};
        }
        double[][] corners = new double[4][];
        try {
            for (int i = 0; i < 4; i++) {
                corners[i] = Geometry.intersect(lines[(i + 3) % 4], lines[i]);
            }
        } catch (ArithmeticException e) {
            throw new NoScreen("outline edges are parallel");
        }
        // order clockwise from the top edge (camera roughly upright)
        double mx = 0, my = 0;
        for (double[] c : corners) {
            mx += c[0] / 4;
            my += c[1] / 4;
        }
        double[] a2 = new double[4];
        for (int i = 0; i < 4; i++) {
            a2[i] = Math.atan2(corners[i][1] - my, corners[i][0] - mx);
        }
        corners = sortBy(corners, a2);
        int top = 0;
        double best = Double.MAX_VALUE;
        for (int i = 0; i < 4; i++) {
            double mid = corners[i][1] + corners[(i + 1) % 4][1];
            if (mid < best) {
                best = mid;
                top = i;
            }
        }
        double[][] rolled = new double[4][];
        for (int i = 0; i < 4; i++) {
            rolled[i] = corners[(i + top) % 4];
        }
        corners = rolled;
        for (int i = 0; i < 4; i++) {
            double[] p = corners[i], q = corners[(i + 1) % 4], r = corners[(i + 2) % 4];
            double ux = q[0] - p[0], uy = q[1] - p[1], vx = r[0] - q[0], vy = r[1] - q[1];
            if (ux * vy - uy * vx <= 0) {
                throw new NoScreen("outline is not convex");
            }
        }
        double margin = 0.15;
        for (double[] c : corners) {
            if (c[0] < -margin * w || c[0] > (1 + margin) * w || c[1] < -margin * h || c[1] > (1 + margin) * h) {
                throw new NoScreen("screen extends too far outside the camera frame");
            }
        }
        return new Found(corners, frac, inside, outside, maxRes);
    }

    private static double[][] sortBy(double[][] items, double[] keys) {
        Integer[] idx = {0, 1, 2, 3};
        final double[] k = keys;
        java.util.Arrays.sort(idx, new java.util.Comparator<Integer>() {
            @Override
            public int compare(Integer a, Integer b) {
                return Double.compare(k[a], k[b]);
            }
        });
        double[][] out = new double[4][];
        for (int i = 0; i < 4; i++) {
            out[i] = items[idx[i]];
        }
        return out;
    }

    /**
     * blob plus every non-blob region that does not reach the image border (screenfind._fill_holes):
     * static things inside the picture (a logo, the setup screen's panel) must not add outlines.
     */
    static boolean[] fillHoles(boolean[] blob, int w, int h) {
        boolean[] outside = new boolean[w * h];
        int[] stack = new int[w * h];
        int sp = 0;
        for (int x = 0; x < w; x++) {
            for (int y : new int[] {0, h - 1}) {
                int i = y * w + x;
                if (!blob[i] && !outside[i]) {
                    outside[i] = true;
                    stack[sp++] = i;
                }
            }
        }
        for (int y = 0; y < h; y++) {
            for (int x : new int[] {0, w - 1}) {
                int i = y * w + x;
                if (!blob[i] && !outside[i]) {
                    outside[i] = true;
                    stack[sp++] = i;
                }
            }
        }
        while (sp > 0) {
            int i = stack[--sp];
            int x = i % w, y = i / w;
            int[] nb = {y > 0 ? i - w : -1, y < h - 1 ? i + w : -1, x > 0 ? i - 1 : -1, x < w - 1 ? i + 1 : -1};
            for (int j : nb) {
                if (j >= 0 && !blob[j] && !outside[j]) {
                    outside[j] = true;
                    stack[sp++] = j;
                }
            }
        }
        boolean[] solid = new boolean[w * h];
        for (int i = 0; i < solid.length; i++) {
            solid[i] = !outside[i];
        }
        return solid;
    }

    /** Connected region of mask around its densest spot, 4-connectivity (screenfind._blob). */
    static boolean[] blob(boolean[] mask, int w, int h) {
        float[] m = new float[w * h];
        for (int i = 0; i < m.length; i++) {
            m[i] = mask[i] ? 1f : 0f;
        }
        float[] dense = box(m, w, h, 8);
        int seed = 0;
        for (int i = 1; i < dense.length; i++) {
            if (dense[i] > dense[seed]) {
                seed = i;
            }
        }
        boolean[] out = new boolean[w * h];
        if (!mask[seed]) {
            return out;
        }
        int[] stack = new int[w * h];
        int sp = 0;
        stack[sp++] = seed;
        out[seed] = true;
        while (sp > 0) {
            int i = stack[--sp];
            int x = i % w, y = i / w;
            int[] nb = {y > 0 ? i - w : -1, y < h - 1 ? i + w : -1, x > 0 ? i - 1 : -1, x < w - 1 ? i + 1 : -1};
            for (int j : nb) {
                if (j >= 0 && mask[j] && !out[j]) {
                    out[j] = true;
                    stack[sp++] = j;
                }
            }
        }
        return out;
    }

    /** The `count` strongest lines through the points, as (theta, rho, votes) (screenfind._hough_lines). */
    static List<double[]> hough(List<double[]> pts, int w, int h, int count) {
        int diag = (int) Math.ceil(Math.hypot(h, w));
        int nr = 2 * diag + 1;
        float[] acc = new float[180 * nr];
        double[] cs = new double[180], sn = new double[180];
        for (int t = 0; t < 180; t++) {
            cs[t] = Math.cos(Math.toRadians(t));
            sn[t] = Math.sin(Math.toRadians(t));
        }
        for (double[] p : pts) {
            for (int t = 0; t < 180; t++) {
                int r = (int) Math.rint(p[0] * cs[t] + p[1] * sn[t]) + diag;
                acc[t * nr + r] += 1;
            }
        }
        float[] sm = acc.clone();                       // a pixel staircase straddles two bins
        for (int t = 0; t < 180; t++) {
            for (int r = 1; r < nr - 1; r++) {
                sm[t * nr + r] = acc[t * nr + r - 1] + acc[t * nr + r] + acc[t * nr + r + 1];
            }
        }
        acc = sm;
        List<double[]> lines = new ArrayList<double[]>();
        for (int c = 0; c < count; c++) {
            int best = 0;
            for (int i = 1; i < acc.length; i++) {
                if (acc[i] > acc[best]) {
                    best = i;
                }
            }
            int ti = best / nr, ri = best % nr;
            lines.add(new double[] {Math.toRadians(ti), ri - diag, acc[best]});
            for (int dt = -12; dt <= 12; dt++) {        // clear the peak and its near-duplicates
                int t = ti + dt, r = ri;
                if (t < 0 || t >= 180) {                // theta wraps with rho mirrored
                    t = ((t % 180) + 180) % 180;
                    r = 2 * diag - ri;
                }
                for (int k = Math.max(0, r - 10); k < Math.min(nr, r + 11); k++) {
                    acc[t * nr + k] = 0;
                }
            }
        }
        return lines;
    }

    /** Total-least-squares line through points: (cx, cy, dx, dy). */
    static double[] fitLine(List<double[]> p) {
        double cx = 0, cy = 0;
        for (double[] q : p) {
            cx += q[0];
            cy += q[1];
        }
        cx /= p.size();
        cy /= p.size();
        double sxx = 0, sxy = 0, syy = 0;
        for (double[] q : p) {
            double dx = q[0] - cx, dy = q[1] - cy;
            sxx += dx * dx;
            sxy += dx * dy;
            syy += dy * dy;
        }
        double theta = 0.5 * Math.atan2(2 * sxy, sxx - syy);   // direction of the largest spread
        return new double[] {cx, cy, Math.cos(theta), Math.sin(theta)};
    }
}
