// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Live break detection from zone fingerprints; a Java port of detector/detect.py, giving the same
 * decisions on the same input (checked by ReplayMain against detector/replay.py).
 *
 * Frames are put on a fixed RATE grid (skipped slots take the nearest frame, two frames in one
 * slot keep the newer). For each template the newest stretch is scored on its best KEEP fraction
 * of frames, each template frame allowed +-1 slot of timing slack. State machine:
 *
 *   not in break --"start" template--> in break --"end" template--> not in break
 *
 * Fails open (back to sound) when a break the detector muted runs too long, the channel changes
 * or the picture stops; a break the user started by muting is ended only by an "end" template.
 *
 * Cost: when a slot is added, its features are correlated once with every template frame; the
 * scores reuse those dot products for as long as the slot stays in the window, so a frame costs
 * (template frames x 432) multiplications rather than (template frames x window x 432).
 */
public final class Detector {
    public static final double RATE = 5.0;
    static final float THRESHOLD = 0.8f;
    static final double COOLDOWN_S = 15, MAX_BREAK_S = 8 * 60, SETTLE_S = 10, GAP_S = 10, RESET_GAP_S = 2.0;
    static final float KEEP = 0.6f, WEIGHT_MIN = 0.3f;
    static final int BUFFER = (int) (12 * RATE);

    private List<Template> templates = new ArrayList<Template>();
    private int[] offset;             // start of each template's frames in the dot-product vectors
    private int totalFrames;
    private String channel;

    private boolean inBreak;
    private double breakSince;
    private String startedBy;         // "detector" or "user"
    private final Map<String, Double> lastFired = new HashMap<String, Double>();

    // 5 fps grid: ring of slots, each holding the dot products of its frame with every template frame
    private final float[][] slotDots = new float[BUFFER][];
    private int slots;                // number of valid slots (<= BUFFER), newest at index head
    private int head = -1;
    private boolean haveGrid;
    private double gridT0, lastT = Double.NaN;
    private long lastSlot;
    private final float[] feat = new float[Features.DIM];

    public boolean inBreak() {
        return inBreak;
    }

    public String channel() {
        return channel;
    }

    /** New channel or a new template set for it; a running break on another channel fails open. */
    public List<Action> setChannel(String name, List<Template> set, double t) {
        List<Action> out = new ArrayList<Action>();
        if (name == null ? channel != null : !name.equals(channel)) {
            if (inBreak) {
                failOpen(t, "channel changed", out);
            }
            channel = name;
        }
        setTemplates(set);
        return out;
    }

    /** Replaces the template set (same channel), e.g. after a library update. */
    public void setTemplates(List<Template> set) {
        templates = new ArrayList<Template>(set);
        offset = new int[templates.size()];
        totalFrames = 0;
        for (int i = 0; i < templates.size(); i++) {
            offset[i] = totalFrames;
            totalFrames += templates.get(i).frames;
        }
        clearGrid();                  // cached dot products belong to the old set
    }

    /** The user muted or unmuted by hand: follow them until the next template. */
    public void userOverride(boolean muted, double t) {
        if (!muted && inBreak) {
            reset();
        } else if (muted && !inBreak) {
            inBreak = true;
            breakSince = t;
            startedBy = "user";
        }
    }

    /** Call regularly even without frames, so a stalled picture still ends a break. */
    public List<Action> tick(double t) {
        List<Action> out = new ArrayList<Action>();
        if (inBreak && !Double.isNaN(lastT) && t - lastT > GAP_S) {
            failOpen(t, "picture lost", out);
        } else if (inBreak && t - breakSince > MAX_BREAK_S) {
            failOpen(t, "break too long", out);
        }
        return out;
    }

    /** One fingerprint (432 bytes at off) taken at epoch t. */
    public List<Action> push(double t, byte[] zones, int off) {
        List<Action> out = tick(t);
        float w = Features.compute(zones, off, feat);
        gridPut(t);
        lastT = t;
        if (templates.isEmpty() || slots == 0) {
            return out;
        }
        for (int ti = 0; ti < templates.size(); ti++) {
            Template tp = templates.get(ti);
            int m = tp.frames;
            if (slots < m + 1) {
                continue;
            }
            float score = Math.max(score(ti, tp, 0), score(ti, tp, 1));
            float thr = tp.threshold > 0 ? tp.threshold : THRESHOLD;
            if (score < thr) {
                continue;
            }
            Double last = lastFired.get(tp.name);
            if (last != null && t - last < COOLDOWN_S) {
                continue;
            }
            lastFired.put(tp.name, t);
            out.add(new Action(Action.HIT, t, tp.name, tp.kind, score, null));
            onHit(tp, t, out);
        }
        return out;
    }

    // ------------------------------------------------------------ scoring

    /** Dot product of template frame i with window column j (0..m, m = newest slot). */
    private float dot(int ti, int i, int j, int m) {
        float[] d = slotDots[(head - (m - j) + BUFFER) % BUFFER];
        return d[offset[ti] + i];
    }

    /** Template aligned so its frame i sits at window column i + o; robust mean of the best frames. */
    private float score(int ti, Template tp, int o) {
        int m = tp.frames;
        float[] v = new float[m];
        for (int i = 0; i < m; i++) {
            if (tp.weight[i] <= WEIGHT_MIN) {
                v[i] = -1f;
                continue;
            }
            int j = i + o;
            float best = dot(ti, i, j, m);
            if (j > 0) {
                best = Math.max(best, dot(ti, i, j - 1, m));
            }
            if (j < m) {
                best = Math.max(best, dot(ti, i, j + 1, m));
            }
            v[i] = best;
        }
        int k = Math.max(1, Math.round(KEEP * m));
        java.util.Arrays.sort(v);
        float s = 0;
        for (int i = m - k; i < m; i++) {
            s += v[i];
        }
        return s / k;
    }

    // ------------------------------------------------------------ state machine

    private void onHit(Template tp, double t, List<Action> out) {
        if ("start".equals(tp.kind) && !inBreak) {
            inBreak = true;
            breakSince = t;
            startedBy = "detector";
            out.add(new Action(Action.MUTE, t, tp.name, null, 0, "mute marker"));
        } else if ("end".equals(tp.kind) && inBreak && t - breakSince > SETTLE_S) {
            reset();
            out.add(new Action(Action.UNMUTE, t, tp.name, null, 0, "unmute marker"));
        }
    }

    private void reset() {
        inBreak = false;
        startedBy = null;
    }

    /** Ends the break; unmutes only if the detector muted. */
    private void failOpen(double t, String reason, List<Action> out) {
        boolean ours = "detector".equals(startedBy);
        reset();
        if (ours) {
            out.add(new Action(Action.UNMUTE, t, null, null, 0, reason));
        }
    }

    // ------------------------------------------------------------ 5 fps grid

    private void clearGrid() {
        slots = 0;
        head = -1;
        haveGrid = false;
    }

    private float[] dotsOfCurrent() {
        float[] d = new float[totalFrames];
        for (int ti = 0; ti < templates.size(); ti++) {
            Template tp = templates.get(ti);
            for (int i = 0; i < tp.frames; i++) {
                d[offset[ti] + i] = Features.dot(tp.feat, i * Features.DIM, feat, 0);
            }
        }
        return d;
    }

    private void append(float[] d) {
        head = (head + 1) % BUFFER;
        slotDots[head] = d;
        if (slots < BUFFER) {
            slots++;
        }
    }

    private void gridPut(double t) {
        if (!haveGrid || (!Double.isNaN(lastT) && t - lastT > RESET_GAP_S)) {
            slots = 0;                // gap: old frames no longer adjoin new ones
            head = -1;
            gridT0 = t;
            lastSlot = -1;
            haveGrid = true;
        }
        long k = Math.round((t - gridT0) * RATE);
        float[] d = dotsOfCurrent();
        if (k <= lastSlot) {          // two frames in one slot: keep the newer
            if (slots > 0) {
                slotDots[head] = d;
            }
            return;
        }
        float[] prev = slots > 0 ? slotDots[head] : null;
        for (long s = lastSlot + 1; s < k; s++) {     // skipped slot: nearest frame fills it
            append(prev != null && (s - lastSlot) <= (k - s) ? prev : d);
        }
        append(d);
        lastSlot = k;
    }
}
