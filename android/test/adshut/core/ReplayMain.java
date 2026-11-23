// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;

/**
 * Laptop check of the Java detector: replays recorder fingerprints (data/fp/DAY.bin) through it
 * with templates exported by detector/export_templates.py, printing every action like
 * detector/replay.py does, so the two can be compared line by line.
 *
 *   java -cp build/test adshut.core.ReplayMain FP_BIN TEMPLATE_DIR CHANNEL
 */
public final class ReplayMain {
    public static void main(String[] args) throws IOException {
        byte[] fp = Files.readAllBytes(new File(args[0]).toPath());
        List<Template> set = new ArrayList<Template>();
        BufferedReader r = new BufferedReader(new FileReader(new File(args[1], "index.tsv")));
        for (String line; (line = r.readLine()) != null; ) {
            String[] f = line.split("\t");
            if (!f[4].equals(args[2])) {
                continue;
            }
            byte[] z = Files.readAllBytes(new File(args[1], f[0] + ".zones").toPath());
            set.add(new Template(f[0], f[1], Float.parseFloat(f[2]), z));
        }
        r.close();
        int rec = 8 + Features.BYTES, n = fp.length / rec;
        ByteBuffer bb = ByteBuffer.wrap(fp).order(ByteOrder.LITTLE_ENDIAN);
        Detector det = new Detector();
        SimpleDateFormat hms = new SimpleDateFormat("HH:mm:ss.S");
        long t0 = System.nanoTime();
        boolean first = true;
        for (int i = 0; i < n; i++) {
            double t = bb.getDouble(i * rec);
            List<Action> acts = new ArrayList<Action>();
            if (first) {
                acts.addAll(det.setChannel(args[2], set, t));
                first = false;
            }
            acts.addAll(det.push(t, fp, i * rec + 8));
            for (Action a : acts) {
                String ts = hms.format(new Date((long) (a.t * 1000)));
                if (Action.HIT.equals(a.type)) {
                    System.out.printf("%s    hit     %-8s %.3f %s%n", ts, a.kind, a.score, a.template);
                } else {
                    System.out.printf("%s  DETECTOR  %-6s  (%s)%n", ts, a.type, a.reason);
                }
            }
        }
        double ms = (System.nanoTime() - t0) / 1e6;
        System.out.printf("%d frames, %d templates in %.1f s (%.3f ms per frame)%n", n, set.size(), ms / 1000, ms / n);
    }
}
