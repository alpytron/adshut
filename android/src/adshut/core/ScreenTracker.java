// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

import java.util.ArrayList;
import java.util.List;

/**
 * Keeps the screen corners current while frames flow in (recorder/screenfind.ScreenTracker).
 * Corners are TL, TR, BR, BL as fractions of the camera frame. Every `window` frames the outline
 * is measured; the calibration changes only when several measurements agree: "moved" (confirm
 * consecutive measurements away from the current corners but close to each other; a letterboxed
 * picture is ignored) or "refined" (the median of `refine` nearby measurements differs a little).
 */
public final class ScreenTracker {
    public static final int WINDOW = 300, CONFIRM = 3, REFINE = 8;
    static final double DEADBAND = 0.02, REFINE_STEP = 0.006;

    private double[][] corners;
    private final ScreenFinder.Activity activity = new ScreenFinder.Activity();
    private int frames;
    private final List<double[][]> near = new ArrayList<double[][]>(), away = new ArrayList<double[][]>();
    /** One line about the latest measurement, set once per window; the caller may clear it. */
    public String status;

    public ScreenTracker(double[][] corners) {
        this.corners = copy(corners);
    }

    public double[][] corners() {
        return copy(corners);
    }

    /** Result of a calibration change. */
    public static final class Change {
        public final String kind;
        public final double[][] corners, previous;
        public final double shift;

        Change(String kind, double[][] corners, double[][] previous, double shift) {
            this.kind = kind;
            this.corners = corners;
            this.previous = previous;
            this.shift = shift;
        }
    }

    /** Largest corner displacement, as a fraction of frame width (16:9 camera frame). */
    static double distance(double[][] a, double[][] b) {
        double m = 0;
        for (int i = 0; i < 4; i++) {
            m = Math.max(m, Math.hypot(a[i][0] - b[i][0], (a[i][1] - b[i][1]) * 9 / 16));
        }
        return m;
    }

    boolean isLetterbox(double[][] cand) {
        double[] h = Geometry.homography(corners, Geometry.UNIT_SQUARE);
        double[][] r = new double[4][];
        for (int i = 0; i < 4; i++) {
            r[i] = Geometry.apply(h, cand[i][0], cand[i][1]);
        }
        double tol = 0.035;
        double left = (r[0][0] + r[3][0]) / 2, right = (r[1][0] + r[2][0]) / 2;
        double top = (r[0][1] + r[1][1]) / 2, bottom = (r[3][1] + r[2][1]) / 2;
        double skew = Math.max(Math.max(Math.abs(r[0][0] - r[3][0]), Math.abs(r[1][0] - r[2][0])),
                Math.max(Math.abs(r[0][1] - r[1][1]), Math.abs(r[3][1] - r[2][1])));
        if (skew > tol) {
            return false;
        }
        boolean xSame = Math.abs(left) < tol && Math.abs(right - 1) < tol;
        boolean ySame = Math.abs(top) < tol && Math.abs(bottom - 1) < tol;
        boolean xBars = left > tol && right < 1 - tol;
        boolean yBars = top > tol && bottom < 1 - tol;
        return (xSame && yBars) || (ySame && xBars);
    }

    /** Feeds one luma frame (w x h); returns a Change when the calibration changes, else null. */
    public Change add(float[] luma, int w, int h) {
        activity.add(luma, w, h);
        if (++frames < WINDOW) {
            return null;
        }
        frames = 0;
        float[] map = activity.take();
        if (map == null) {
            return null;
        }
        ScreenFinder.Found f;
        try {
            f = ScreenFinder.find(map, w, h);
        } catch (ScreenFinder.NoScreen e) {
            status = "no measurement: " + e.getMessage();
            return null;
        }
        double[][] cand = new double[4][];
        for (int i = 0; i < 4; i++) {
            cand[i] = new double[] {f.corners[i][0] / w, f.corners[i][1] / h};
        }
        double dist = distance(cand, corners);
        status = String.format("measured %.4f from current corners", dist);
        if (dist <= DEADBAND) {
            away.clear();
            near.add(cand);
            status += String.format(" (near %d/%d)", near.size(), REFINE);
            if (near.size() < REFINE) {
                return null;
            }
            double[][] med = median(near);
            near.clear();
            if (distance(med, corners) < REFINE_STEP) {
                return null;
            }
            return adopt(med, "refined");
        }
        if (isLetterbox(cand)) {
            status += ", ignored: black bars on screen";
            return null;
        }
        if (!away.isEmpty() && distance(cand, away.get(away.size() - 1)) > 0.75 * DEADBAND) {
            away.clear();                       // still moving, or a one-off bad fit: start over
        }
        away.add(cand);
        status += String.format(" (away %d/%d)", away.size(), CONFIRM);
        if (away.size() < CONFIRM) {
            return null;
        }
        return adopt(median(away), "moved");
    }

    private Change adopt(double[][] c, String kind) {
        double[][] old = corners;
        corners = c;
        near.clear();
        away.clear();
        return new Change(kind, copy(c), old, distance(c, old));
    }

    private static double[][] median(List<double[][]> list) {
        double[][] out = new double[4][2];
        double[] v = new double[list.size()];
        for (int i = 0; i < 4; i++) {
            for (int k = 0; k < 2; k++) {
                for (int j = 0; j < list.size(); j++) {
                    v[j] = list.get(j)[i][k];
                }
                out[i][k] = Geometry.median(v);
            }
        }
        return out;
    }

    private static double[][] copy(double[][] c) {
        double[][] o = new double[4][];
        for (int i = 0; i < 4; i++) {
            o[i] = c[i].clone();
        }
        return o;
    }
}
