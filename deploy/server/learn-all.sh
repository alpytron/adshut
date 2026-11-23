#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
# Runs the learner for every TV that uploads to the template server (DATA/tvs/<id>), all sharing
# one template library, plus this machine's own host data while adshut-host runs here.
set -u
D=${ADSHUT_DATA:-$HOME/.local/share/adshut}
export PYTHONPATH=${PYTHONPATH:-$HOME/adshut/python}
if systemctl --user is-active --quiet adshut-host; then
    python3 -m adshut.learn --data "$D"
fi
for tv in "$D"/tvs/*/; do
    [ -d "$tv/fp" ] || continue
    echo "== $(basename "$tv")"
    python3 -m adshut.learn --data "$tv" --templates "$D/templates"
done
