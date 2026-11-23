// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

/**
 * Camera setup measurements: while the setup screen shows a slowly alternating pattern on the
 * whole screen, frames go to add(); every MEASURE_MS the activity map is evaluated with the
 * screen finder and the result is turned into readouts and plain-language hints.
 */
public final class Setup {
    public static final long MEASURE_MS = 4000;

    private final ScreenFinder.Activity activity = new ScreenFinder.Activity();
    private long windowStart = -1;

    /** Latest result (null corners when the screen was not found). */
    public static final class Reading {
        public final double[][] corners;      // fractions of the camera frame, TL TR BR BL, or null
        public final String problem;          // why nothing was found, or null
        public final double coverage;         // screen width / camera width
        public final double sideRatio, vertRatio, tiltDeg;
        public final double clipped, meanLuma;
        public final String[] hints;
        public final long at;

        Reading(double[][] c, String problem, double coverage, double sideRatio, double vertRatio, double tiltDeg,
                double clipped, double meanLuma, String[] hints, long at) {
            this.corners = c;
            this.problem = problem;
            this.coverage = coverage;
            this.sideRatio = sideRatio;
            this.vertRatio = vertRatio;
            this.tiltDeg = tiltDeg;
            this.clipped = clipped;
            this.meanLuma = meanLuma;
            this.hints = hints;
            this.at = at;
        }
    }

    /** Feeds one half-size luma frame; returns a new Reading every MEASURE_MS, else null. */
    public Reading add(float[] luma, int w, int h, long nowMs) {
        activity.add(luma, w, h);
        if (windowStart < 0) {
            windowStart = nowMs;
        }
        if (nowMs - windowStart < MEASURE_MS) {
            return null;
        }
        windowStart = nowMs;
        float[] map = activity.take();
        if (map == null) {
            return null;
        }
        ScreenFinder.Found f;
        try {
            f = ScreenFinder.find(map, w, h);
        } catch (ScreenFinder.NoScreen e) {
            return new Reading(null, e.getMessage(), 0, 0, 0, 0, 0, 0,
                    new String[] {"Screen not found: " + e.getMessage(),
                            "Make sure the whole TV screen is in the camera's view"}, nowMs);
        }
        return evaluate(f.corners, luma, w, h, nowMs);
    }

    static Reading evaluate(double[][] px, float[] luma, int w, int h, long nowMs) {
        double[][] c = new double[4][];
        for (int i = 0; i < 4; i++) {
            c[i] = new double[] {px[i][0] / w, px[i][1] / h};
        }
        double top = len(px[0], px[1]), bottom = len(px[3], px[2]);
        double left = len(px[0], px[3]), right = len(px[1], px[2]);
        double coverage = (top + bottom) / 2 / w;
        double sideRatio = Math.min(left, right) / Math.max(left, right);    // 1 = straight on
        double vertRatio = Math.min(top, bottom) / Math.max(top, bottom);
        double tilt = Math.toDegrees(Math.atan2(px[1][1] - px[0][1], px[1][0] - px[0][0]))
                - 0;   // top edge angle; perspective also slants it, so only large values mean a tilted camera
        // exposure inside the screen area
        double[] hm = Geometry.homography(new double[][] {{0, 0}, {1, 0}, {1, 1}, {0, 1}}, px);
        int n = 0, clip = 0;
        double sum = 0;
        for (int gy = 1; gy < 20; gy++) {
            for (int gx = 1; gx < 20; gx++) {
                double[] p = Geometry.apply(hm, gx / 20.0, gy / 20.0);
                int x = (int) p[0], y = (int) p[1];
                if (x < 0 || y < 0 || x >= w || y >= h) {
                    continue;
                }
                float v = luma[y * w + x];
                sum += v;
                n++;
                if (v >= 250) {
                    clip++;
                }
            }
        }
        double mean = n == 0 ? 0 : sum / n, clipped = n == 0 ? 0 : (double) clip / n;
        java.util.List<String> hints = new java.util.ArrayList<String>();
        String[] names = {"top-left", "top-right", "bottom-right", "bottom-left"};
        for (int i = 0; i < 4; i++) {
            if (c[i][0] < 0.01 || c[i][0] > 0.99 || c[i][1] < 0.01 || c[i][1] > 0.99) {
                hints.add("The " + names[i] + " corner is at the edge of the camera's view: aim "
                        + (c[i][0] < 0.5 ? "more left" : "more right") + " / " + (c[i][1] < 0.5 ? "higher" : "lower"));
            }
        }
        if (coverage < 0.3) {
            hints.add("The screen is small in the picture: move the camera closer or use a narrower view");
        } else if (coverage > 0.9) {
            hints.add("The screen nearly fills the picture: a little more margin helps if the camera moves");
        }
        if (sideRatio < 0.6) {
            hints.add("Steep side angle: place the camera more in front of the screen");
        }
        if (vertRatio < 0.6) {
            hints.add("Steep vertical angle: raise or lower the camera towards screen height");
        }
        if (Math.abs(tilt) > 30) {
            hints.add("The camera looks tilted: turn it upright");
        }
        if (clipped > 0.05) {
            hints.add("Glare or overexposure on the screen: change the angle or reduce light behind the camera");
        } else if (mean < 30) {
            hints.add("The screen looks very dark to the camera");
        }
        if (hints.isEmpty()) {
            hints.add("Good placement. Press OK to save these corners.");
        }
        return new Reading(c, null, coverage, sideRatio, vertRatio, tilt, clipped, mean,
                hints.toArray(new String[0]), nowMs);
    }

    private static double len(double[] a, double[] b) {
        return Math.hypot(a[0] - b[0], a[1] - b[1]);
    }

    /** Elementwise median of several corner sets (fractions). */
    public static double[][] median(java.util.List<double[][]> sets) {
        double[][] out = new double[4][2];
        double[] v = new double[sets.size()];
        for (int i = 0; i < 4; i++) {
            for (int k = 0; k < 2; k++) {
                for (int j = 0; j < sets.size(); j++) {
                    v[j] = sets.get(j)[i][k];
                }
                out[i][k] = Geometry.median(v);
            }
        }
        return out;
    }
}
