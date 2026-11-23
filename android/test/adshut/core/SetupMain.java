// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

import java.io.File;
import java.nio.file.Files;

/** Laptop check of the setup readouts: feeds recorded luma frames (u8, W x H) at 5 fps. */
public final class SetupMain {
    public static void main(String[] a) throws Exception {
        byte[] all = Files.readAllBytes(new File(a[0]).toPath());
        int w = Integer.parseInt(a[1]), h = Integer.parseInt(a[2]);
        Setup s = new Setup();
        float[] luma = new float[w * h];
        for (int f = 0; f < all.length / (w * h); f++) {
            for (int i = 0; i < w * h; i++) {
                luma[i] = all[f * w * h + i] & 0xff;
            }
            Setup.Reading r = s.add(luma, w, h, f * 200L);
            if (r == null) {
                continue;
            }
            if (r.corners == null) {
                System.out.printf("%5d not found: %s%n", f, r.problem);
                continue;
            }
            StringBuilder cs = new StringBuilder();
            for (double[] c : r.corners) {
                cs.append(String.format("[%.1f, %.1f] ", c[0] * w, c[1] * h));
            }
            System.out.print(cs);
            System.out.printf("%5d coverage %.0f%%  side %.2f  vertical %.2f  tilt %.0f°  brightness %.0f  clipped %.0f%%  | %s%n",
                    f, 100 * r.coverage, r.sideRatio, r.vertRatio, r.tiltDeg, r.meanLuma, 100 * r.clipped, String.join(" / ", r.hints));
        }
    }
}
