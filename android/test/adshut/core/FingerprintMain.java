// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

import java.io.File;
import java.nio.file.Files;

/**
 * Laptop check of the fingerprinter: Y (W x H) plus Cb, Cr (W/2 x H/2) raw planes and four
 * corners (fractions) in, the 432-byte fingerprint out (to OUT).
 *
 *   java -cp build/test adshut.core.FingerprintMain Y U V W H x0 y0 x1 y1 x2 y2 x3 y3 OUT
 */
public final class FingerprintMain {
    public static void main(String[] a) throws Exception {
        byte[] yp = Files.readAllBytes(new File(a[0]).toPath());
        byte[] up = Files.readAllBytes(new File(a[1]).toPath());
        byte[] vp = Files.readAllBytes(new File(a[2]).toPath());
        int w = Integer.parseInt(a[3]), h = Integer.parseInt(a[4]);
        double[][] c = new double[4][2];
        for (int i = 0; i < 4; i++) {
            c[i][0] = Double.parseDouble(a[5 + 2 * i]);
            c[i][1] = Double.parseDouble(a[6 + 2 * i]);
        }
        Fingerprinter f = new Fingerprinter(w / 2, h / 2);
        f.setCorners(c);
        byte[] out = new byte[Features.BYTES];
        long t0 = System.nanoTime();
        int reps = 200;
        for (int r = 0; r < reps; r++) {
            f.load(yp, w, up, w / 2, 1, vp, w / 2, 1);
            f.fingerprint(out, 0);
        }
        System.out.printf("%.2f ms per frame (load + fingerprint)%n", (System.nanoTime() - t0) / 1e6 / reps);
        Files.write(new File(a[13]).toPath(), out);
    }
}
