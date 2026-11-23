#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
# Installs the AdShut host on a Raspberry Pi (Raspberry Pi OS / Debian): packages, code in
# ~/adshut, configuration in ~/.config/adshut, a systemd user service. Run on the Pi from a
# checkout of the repository:
#     deploy/rpi/install.sh
# Then edit ~/.config/adshut/host.json (camera, IR or CEC commands, server token) and
#     systemctl --user enable --now adshut-host
set -euo pipefail
cd "$(dirname "$0")/../.."
sudo apt-get install -y python3-numpy python3-pil ffmpeg v4l-utils ir-keytable cec-utils
mkdir -p ~/adshut ~/.config/adshut ~/.config/systemd/user
rsync -a --delete --exclude __pycache__ --exclude tests python/ ~/adshut/python/
cp deploy/rpi/adshut-host.service ~/.config/systemd/user/
[ -f ~/.config/adshut/host.json ] || cp deploy/rpi/host.example.json ~/.config/adshut/host.json
systemctl --user daemon-reload
sudo loginctl enable-linger "$USER"       # run without anyone logged in
echo "Installed. Edit ~/.config/adshut/host.json, then: systemctl --user enable --now adshut-host"
