// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The Java core against spec/fixtures (the Python implementation runs the same fixtures in
 * python/tests/test_parity.py). Exit code 0 when everything matches.
 *
 *   java -cp build/test adshut.core.ParityTest ../spec/fixtures
 */
public final class ParityTest {
    static File fix;
    static int failed;

    public static void main(String[] a) throws Exception {
        fix = new File(a[0]);
        run("features", new Check() { public String run() throws Exception { return features(); } });
        run("detector", new Check() { public String run() throws Exception { return detector(); } });
        run("activity", new Check() { public String run() throws Exception { return activity(); } });
        run("screen", new Check() { public String run() throws Exception { return screen(); } });
        run("fingerprint", new Check() { public String run() throws Exception { return fingerprint(); } });
        System.exit(failed == 0 ? 0 : 1);
    }

    interface Check {
        String run() throws Exception;
    }

    static void run(String name, Check c) {
        try {
            String err = c.run();
            System.out.println((err == null ? "PASS " : "FAIL ") + name + (err == null ? "" : ": " + err));
            if (err != null) {
                failed++;
            }
        } catch (Exception e) {
            System.out.println("FAIL " + name + ": " + e);
            failed++;
        }
    }

    static byte[] bytes(String name) throws Exception {
        return Files.readAllBytes(new File(fix, name).toPath());
    }

    static float[] f32(String name) throws Exception {
        ByteBuffer b = ByteBuffer.wrap(bytes(name)).order(ByteOrder.LITTLE_ENDIAN);
        float[] out = new float[b.remaining() / 4];
        b.asFloatBuffer().get(out);
        return out;
    }

    static String features() throws Exception {
        byte[] s = bytes("stream.bin");
        float[] want = f32("features.f32"), wantW = f32("weights.f32");
        float[] f = new float[Features.DIM];
        double worst = 0;
        for (int i = 0; i < 200; i++) {
            float w = Features.compute(s, i * 440 + 8, f);
            worst = Math.max(worst, Math.abs(w - wantW[i]));
            for (int k = 0; k < Features.DIM; k++) {
                worst = Math.max(worst, Math.abs(f[k] - want[i * Features.DIM + k]));
            }
        }
        return worst < 1e-4 ? null : "max difference " + worst;
    }

    static String detector() throws Exception {
        List<Template> set = new ArrayList<Template>();
        String channel = null;
        BufferedReader r = new BufferedReader(new InputStreamReader(new FileInputStream(new File(fix, "templates/index.tsv")), StandardCharsets.UTF_8));
        for (String line; (line = r.readLine()) != null; ) {
            String[] p = line.split("\t");
            set.add(new Template(p[0], p[1], Float.parseFloat(p[2]), bytes("templates/" + p[0] + ".zones")));
            channel = p[4];
        }
        r.close();
        byte[] s = bytes("stream.bin");
        ByteBuffer bb = ByteBuffer.wrap(s).order(ByteOrder.LITTLE_ENDIAN);
        Detector d = new Detector();
        List<Action> acts = new ArrayList<Action>(d.setChannel(channel, set, bb.getDouble(0)));
        for (int i = 0; i < s.length / 440; i++) {
            acts.addAll(d.push(bb.getDouble(i * 440), s, i * 440 + 8));
        }
        List<String> want = Files.readAllLines(new File(fix, "actions.tsv").toPath(), StandardCharsets.UTF_8);
        if (acts.size() != want.size()) {
            return acts.size() + " actions, expected " + want.size();
        }
        for (int i = 0; i < acts.size(); i++) {
            Action x = acts.get(i);
            String[] w = want.get(i).split("\t", -1);
            boolean same = Math.abs(x.t - Double.parseDouble(w[0])) < 0.002 && x.type.equals(w[1])
                    && (x.template == null ? "" : x.template).equals(w[2])
                    && (Action.HIT.equals(x.type) ? x.kind.equals(w[3]) && Math.abs(x.score - Float.parseFloat(w[4])) < 2e-3 : true);
            if (!same) {
                return "action " + i + ": " + x + " vs " + want.get(i);
            }
        }
        return null;
    }

    static String activity() throws Exception {
        byte[] fr = bytes("luma_20x320x180.u8");
        ScreenFinder.Activity act = new ScreenFinder.Activity();
        float[] l = new float[320 * 180];
        for (int f = 0; f < 20; f++) {
            for (int i = 0; i < l.length; i++) {
                l[i] = fr[f * l.length + i] & 0xff;
            }
            act.add(l, 320, 180);
        }
        float[] got = act.take(), want = f32("activity20.f32");
        double worst = 0;
        for (int i = 0; i < got.length; i++) {
            worst = Math.max(worst, Math.abs(got[i] - want[i]));
        }
        return worst < 1e-3 ? null : "max difference " + worst;
    }

    static String screen() throws Exception {
        ScreenFinder.Found f = ScreenFinder.find(f32("activity.f32"), 320, 180);
        String json = new String(bytes("screen.json"), StandardCharsets.UTF_8);
        Matcher m = Pattern.compile("\\[\\s*([-\\d.]+),\\s*([-\\d.]+)\\s*\\]").matcher(json.substring(json.indexOf("corners_px")));
        double worst = 0;
        for (int i = 0; i < 4 && m.find(); i++) {
            worst = Math.max(worst, Math.max(Math.abs(f.corners[i][0] - Double.parseDouble(m.group(1))),
                    Math.abs(f.corners[i][1] - Double.parseDouble(m.group(2)))));
        }
        return worst < 0.05 ? null : String.format(Locale.US, "corners differ by %.3f px", worst);
    }

    static String fingerprint() throws Exception {
        String json = new String(bytes("camera.json"), StandardCharsets.UTF_8);
        Matcher m = Pattern.compile("\\[\\s*([-\\d.]+),\\s*([-\\d.]+)\\s*\\]").matcher(json);
        double[][] c = new double[4][2];
        for (int i = 0; i < 4 && m.find(); i++) {
            c[i][0] = Double.parseDouble(m.group(1));
            c[i][1] = Double.parseDouble(m.group(2));
        }
        Fingerprinter f = new Fingerprinter(320, 180);
        f.setCorners(c);
        f.load(bytes("camera_y_640x360.u8"), 640, bytes("camera_u_320x180.u8"), 320, 1, bytes("camera_v_320x180.u8"), 320, 1);
        byte[] out = new byte[Features.BYTES];
        f.fingerprint(out, 0);
        byte[] want = bytes("camera.zones");
        int worst = 0;
        for (int i = 0; i < out.length; i++) {
            worst = Math.max(worst, Math.abs((out[i] & 0xff) - (want[i] & 0xff)));
        }
        return worst <= 2 ? null : "a zone differs by " + worst + " levels (YUV path vs JPEG path allows 2)";
    }
}
