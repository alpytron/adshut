// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

import java.io.File;
import java.nio.file.Files;

/**
 * Laptop check of the screen finder: feeds N luma frames (u8, W x H each) to the Activity and
 * prints the corners found for every window of `window` frames, like the Python check does.
 *
 *   java -cp build/test adshut.core.TrackerMain FRAMES.u8 W H WINDOW
 */
public final class TrackerMain {
    public static void main(String[] args) throws Exception {
        byte[] all = Files.readAllBytes(new File(args[0]).toPath());
        int w = Integer.parseInt(args[1]), h = Integer.parseInt(args[2]), win = Integer.parseInt(args[3]);
        int n = all.length / (w * h);
        ScreenFinder.Activity act = new ScreenFinder.Activity();
        float[] luma = new float[w * h];
        long t0 = System.nanoTime();
        for (int f = 0; f < n; f++) {
            for (int i = 0; i < w * h; i++) {
                luma[i] = all[f * w * h + i] & 0xff;
            }
            act.add(luma, w, h);
            if ((f + 1) % win == 0) {
                try {
                    ScreenFinder.Found r = ScreenFinder.find(act.take(), w, h);
                    StringBuilder sb = new StringBuilder();
                    for (double[] c : r.corners) {
                        sb.append(String.format("[%.1f, %.1f] ", c[0], c[1]));
                    }
                    System.out.printf("%5d %s area %.3f inside %.2f outside %.2f rms %.2f%n", f + 1, sb, r.area, r.inside, r.outside, r.edgeRms);
                } catch (ScreenFinder.NoScreen e) {
                    System.out.printf("%5d NoScreen: %s%n", f + 1, e.getMessage());
                }
            }
        }
        System.out.printf("%.2f ms per frame%n", (System.nanoTime() - t0) / 1e6 / n);
    }
}
