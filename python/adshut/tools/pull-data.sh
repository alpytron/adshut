#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
# Mirrors a host's or template server's data directory to this machine, for replays and
# simulations on a faster computer. Read-only use: never run the learner against the mirror;
# the live template library stays on the server.
#   pull-data.sh HOST [REMOTE_DIR] [--thumbs]
# REMOTE_DIR defaults to .local/share/adshut (relative to the remote home). The mirror goes to
# $ADSHUT_MIRROR, default data/ in the repository (gitignored).
set -euo pipefail
HOST=${1:?usage: pull-data.sh HOST [REMOTE_DIR] [--thumbs]}
SRC=.local/share/adshut
THUMBS=0
for a in "${@:2}"; do
    if [ "$a" = "--thumbs" ]; then THUMBS=1; else SRC=$a; fi
done
DST=${ADSHUT_MIRROR:-$(cd "$(dirname "$0")/../../.." && pwd)/data}
mkdir -p "$DST"
EXCL=(--exclude 'thumbs/')
[ "$THUMBS" = 1 ] && EXCL=()
rsync -a --delete "${EXCL[@]}" --exclude 'templates.bak-*' --exclude tokens.json "$HOST:$SRC/" "$DST/"
du -sh "$DST"
