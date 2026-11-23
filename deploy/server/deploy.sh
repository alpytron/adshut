#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
# Deploys the template server (and optionally a host) to a Linux machine over ssh:
#     deploy/server/deploy.sh HOST [HOST_CONFIG.json]
# Code goes to ~/adshut, configuration to ~/.config/adshut (host.json, optional env file with
# ADSHUT_DATA=...), data to $ADSHUT_DATA (default ~/.local/share/adshut). Services (systemd user):
#   adshut-server       template server, port 8766; add TVs with
#                       PYTHONPATH=~/adshut/python python3 -m adshut.server --add-tv ID
#   adshut-learn.timer  learner every 3 minutes
#   adshut-host         only when HOST_CONFIG is given: a live loop running on this machine
set -euo pipefail
cd "$(dirname "$0")/../.."
HOST=${1:?usage: deploy.sh HOST [HOST_CONFIG.json]}
CONFIG=${2:-}

ssh "$HOST" 'mkdir -p ~/adshut/python ~/adshut/deploy/server ~/.config/adshut ~/.config/systemd/user'
rsync -a --delete --exclude __pycache__ --exclude tests python/ "$HOST":adshut/python/
rsync -a --delete --exclude host.json deploy/server/ "$HOST":adshut/deploy/server/
[ -n "$CONFIG" ] && rsync -a "$CONFIG" "$HOST":.config/adshut/host.json
ssh "$HOST" "cd ~/adshut/deploy/server &&
    cp adshut-host.service adshut-learn.service adshut-learn.timer adshut-server.service ~/.config/systemd/user/ &&
    systemctl --user daemon-reload &&
    systemctl --user enable --now adshut-server adshut-learn.timer >/dev/null 2>&1 &&
    systemctl --user restart adshut-server &&
    if [ -f ~/.config/adshut/host.json ]; then
        systemctl --user enable adshut-host >/dev/null 2>&1 && systemctl --user restart adshut-host
    fi &&
    sleep 3 && systemctl --user --no-pager --lines=0 status adshut-server adshut-host 2>/dev/null | grep -E '●|Active'"
