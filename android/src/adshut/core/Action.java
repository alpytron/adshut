// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Alpar Sandor
package adshut.core;

/** Something the detector wants done or logged. */
public final class Action {
    public static final String MUTE = "mute", UNMUTE = "unmute", HIT = "hit";

    public final String type;
    public final double t;
    public final String template;    // null for safety unmutes
    public final String kind;        // template kind, for hits
    public final float score;        // for hits
    public final String reason;      // for mute/unmute

    Action(String type, double t, String template, String kind, float score, String reason) {
        this.type = type;
        this.t = t;
        this.template = template;
        this.kind = kind;
        this.score = score;
        this.reason = reason;
    }

    @Override
    public String toString() {
        return type + (template != null ? " " + template : "") + (reason != null ? " (" + reason + ")" : "")
                + (HIT.equals(type) ? String.format(" %s %.3f", kind, score) : "");
    }
}
