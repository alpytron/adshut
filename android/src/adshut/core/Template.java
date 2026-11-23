// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

/**
 * A reference sequence (ident, card, ...) sampled at Detector.RATE: per-frame features, weights
 * and its metadata. kind: "start" mutes (break begins), "end" unmutes (programme returns).
 */
public final class Template {
    public final String name;
    public final String kind;
    public final float threshold;
    public final int frames;
    final float[] feat;          // frames x Features.DIM
    final float[] weight;        // frames

    public Template(String name, String kind, float threshold, byte[] zones) {
        if (zones.length % Features.BYTES != 0) {
            throw new IllegalArgumentException("zones length " + zones.length + " is not a whole number of frames");
        }
        this.name = name;
        this.kind = kind;
        this.threshold = threshold;
        this.frames = zones.length / Features.BYTES;
        this.feat = new float[frames * Features.DIM];
        this.weight = new float[frames];
        float[] tmp = new float[Features.DIM];
        for (int i = 0; i < frames; i++) {
            weight[i] = Features.compute(zones, i * Features.BYTES, tmp);
            System.arraycopy(tmp, 0, feat, i * Features.DIM, Features.DIM);
        }
    }
}
