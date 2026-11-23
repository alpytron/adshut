#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
# Runs the learner for every TV that uploads to the template server (DATA/tvs/<id>), all sharing
# one template library, plus this machine's own host data while adshut-host runs here.
# ADSHUT_LEARN_ARGS (e.g. "--mine", in ~/.config/adshut/env) is passed to every run; a TV's
# DATA/tvs/<id>/learn.args adds arguments for that TV only (e.g. "--history /path/to/older/data").
set -u
D=${ADSHUT_DATA:-$HOME/.local/share/adshut}
export PYTHONPATH=${PYTHONPATH:-$HOME/adshut/python}
read -r -a ARGS <<< "${ADSHUT_LEARN_ARGS:-}"
if systemctl --user is-active --quiet adshut-host; then
    python3 -m adshut.learn --data "$D" "${ARGS[@]}"
fi
for tv in "$D"/tvs/*/; do
    [ -d "$tv/fp" ] || continue
    echo "== $(basename "$tv")"
    TV_ARGS=()
    [ -f "$tv/learn.args" ] && read -r -a TV_ARGS < "$tv/learn.args"
    python3 -m adshut.learn --data "$tv" --templates "$D/templates" "${ARGS[@]}" "${TV_ARGS[@]}"
done
