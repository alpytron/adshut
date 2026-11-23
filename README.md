# AdShut

**Mutes TV commercials by itself, learning from your own mute button.**

A webcam watches the TV screen. AdShut recognises the short idents and cards that channels air
at the start and end of an ad break, mutes the TV at the opening one and unmutes it at the
closing one. It needs no list of channels or broadcast schedule: it learns what a break looks
like from the times you press mute and unmute yourself, so it works on any channel in any
country.

It runs as an **Android TV app**, or as a **Python service on a Raspberry Pi or Linux PC** that
mutes any TV, smart or not, through an IR blaster or HDMI-CEC.

> Status: early and experimental. It works on the author's TV; expect rough edges, and expect
> to teach it for a few days before it mutes on its own.

## Privacy first

AdShut watches your TV, so its design starts with what it does **not** do.

- **No camera picture ever leaves the TV.** Frames are processed in memory on the device the
  camera is plugged into (the Android TV, or the Raspberry Pi) and thrown away. The TV app does
  not serve camera pictures on the network by default. A switch on the TV's own settings
  screen allows that for a separate host computer, and it cannot be turned on over the network.
- **No audio is ever recorded.** AdShut does not listen at all: it only switches the TV's mute
  on and off.
- **Only unrecognisable fingerprints go to the server.** For each frame, AdShut keeps the
  average colour of 16 × 9 blocks of the screen: 432 bytes, a mosaic far too coarse to show
  any picture, face or text. These fingerprints, with the events that label them (channel
  name, your mute and unmute presses, TV on/off), are all a TV uploads to the template server.
  The server uses them to learn the break markers it sends back to every TV.

What fingerprints and events *can* tell is what was on and when. That is viewing data. Send it
only to a template server you trust, or run your own (it is one Python file, see below). The
project itself runs no server and collects nothing.

## How it works

```
 camera ──> screen finder ──> fingerprint ──> detector ──> mute / unmute
            (finds the TV in   (16x9 colour    (matches
             the picture, any   blocks, 5 per   templates of
             angle)             second)         idents)
                                    │                ▲
                                    ▼                │ templates
                            template server  ── learner
                            (your fingerprints   (learns break markers
                             + mute presses)      from mute presses)
```

1. **Find the screen.** Only the TV picture changes in fine detail from frame to frame, so a
   map of that change, with a Hough-line fit, gives the screen's four corners. The camera can sit
   at any angle and may be moved; the corners follow.
2. **Fingerprint.** The screen area is straightened and reduced to 16 × 9 blocks of
   brightness and colour, five times a second. The comparison ignores brightness, contrast and
   white balance, so it survives a different camera or room light.
3. **Detect.** Each new frame is compared with the channel's templates: a few seconds of
   fingerprints of an opening or closing ident. A match of the opening one mutes and a match of
   the closing one unmutes. Safety rules unmute after 8 minutes, on a channel change, or when
   the picture is lost. Your own presses always win.
4. **Learn.** When you mute by hand, the learner looks at the seconds before your press, finds
   the same pictures in earlier days of recording, and cuts out exactly the stretch that
   repeats: the ident. It keeps only those that, judged by all your past presses, really come
   at a break boundary. A template that makes a wrong call you correct is disabled.

Details: [docs/design.md](docs/design.md). The formats every implementation must agree on are in
[spec/README.md](spec/README.md).

## What you need

| Setup | Camera | Mutes through | Runs |
|---|---|---|---|
| Android TV (Android 6+) | USB webcam with MJPEG (UVC); a Logitech C920 works | the TV itself | the TV app; learning on a template server |
| Any TV | USB webcam or Raspberry Pi camera module on a Raspberry Pi 4/5 or Linux PC | IR LED / USB IR blaster, or HDMI-CEC | the Python host; learning on a template server |
| Template server | none | none | any small Linux machine on your network: Python 3, numpy |

The template server can run on the same Raspberry Pi as a host.

## Quick start

### 1. Template server

```sh
sudo apt install python3-numpy python3-pil
git clone https://github.com/alpytron/adshut.git && cd adshut
deploy/server/deploy.sh my-server           # over ssh; code in ~/adshut, systemd user services
ssh my-server 'PYTHONPATH=~/adshut/python python3 -m adshut.server --add-tv living-room'
                                            # prints that TV's token
```

The server listens on port 8766 and keeps its data in `~/.local/share/adshut` (set
`ADSHUT_DATA` in `~/.config/adshut/env` to change it). It speaks plain HTTP: keep it on your
home network or put it behind an HTTPS reverse proxy.

### 2a. Android TV

Build (Ubuntu/Debian packages only, no Android Studio):

```sh
sudo apt install openjdk-17-jdk-headless android-sdk android-sdk-platform-23 dalvik-exchange
android/build.sh                            # -> android/build/adshut.apk
```

Install with ADB over the network (enable USB debugging in the TV's developer options):

```sh
adb connect <tv-ip>:5555
adb install -r android/build/adshut.apk
adb shell 'P=adshut.app; for p in CAMERA READ_LOGS READ_TV_LISTINGS PACKAGE_USAGE_STATS SYSTEM_ALERT_WINDOW;
    do pm grant $P android.permission.$p; done;
    appops set $P GET_USAGE_STATS allow; appops set $P SYSTEM_ALERT_WINDOW allow'
```

The permissions are needed because the TV reports channel changes only in its log
(`READ_LOGS`), names channels in its channel list (`READ_TV_LISTINGS`), tells the foreground app
only through usage statistics (`PACKAGE_USAGE_STATS`, so AdShut pauses when you leave Live
TV), and lets an app start the camera after boot only from a window (`SYSTEM_ALERT_WINDOW`).

Then open **AdShut** from the TV's launcher:

- The **setup screen** fills the TV with a slowly changing pattern and shows the camera's view
  with the screen outline it found. Aim the camera, and press OK to save.
- **Settings** (DOWN or MENU) takes the template server URL, this TV's id and token, and the
  detector mode. Start with *shadow*: it only shows what it would do. Switch to *active* once
  the decisions look right.

> Some TVs switch their USB port to device mode at boot while USB debugging is on, and the
> webcam disappears. If that happens, turn USB debugging off before switching the TV off.

### 2b. Raspberry Pi with an IR blaster or HDMI-CEC

```sh
git clone https://github.com/alpytron/adshut.git && cd adshut
deploy/rpi/install.sh
$EDITOR ~/.config/adshut/host.json          # camera, IR or CEC commands, server and token
systemctl --user enable --now adshut-host
```

[`deploy/rpi/host.example.json`](deploy/rpi/host.example.json) shows the options:

- an IR LED on a GPIO pin (`dtoverlay=gpio-ir-tx`) with `ir-ctl`, or LIRC `irsend`;
- `cec-client` over HDMI;
- an optional IR receiver (`dtoverlay=gpio-ir`), so the host sees your remote's MUTE presses.
  Most TVs only have a mute *toggle* and report nothing back. The presses keep the host's idea
  of the mute state right, and they are what the learner learns from.

### 3. Teach it

Watch TV as usual and **mute every ad break by hand, then unmute when it ends**. Each press is
a lesson. After a break has aired a few times with the same ident, AdShut recognises it on its
own, and a toast on the TV says what it did. If it gets one wrong, undo it with the remote:
that counts against the template that caused it.

## Repository layout

| Path | What |
|---|---|
| `android/` | the Android TV app; `src/adshut/core` is the Java port of the algorithm core |
| `python/adshut/` | the Python package: core, `host/` (live loop with TV and camera adapters), `learn`, `server`, `sync`, `tools/` |
| `spec/` | formats and synthetic test fixtures both implementations must reproduce |
| `deploy/server/`, `deploy/rpi/` | systemd units, installers, example configs |
| `docs/` | design notes |

## Development

```sh
./tests.sh                                   # Python and Java against the same fixtures
PYTHONPATH=python python3 python/tests/make_fixtures.py   # regenerate fixtures (on purpose only)
PYTHONPATH=python python3 python/tests/e2e_command_host.py # Raspberry-Pi-style host end to end, no hardware (~2 min)
```

There are two implementations of the live loop: Java on the TV and Python on Linux. Each
change to the core must keep them in agreement: `tests.sh` checks the features, the detector's
decisions, the screen finder and the fingerprints against `spec/fixtures`. The end-to-end test
runs a host with a fake camera program, IR blaster, IR receiver and a local template server.
All test material is synthetic, drawn by `python/tests/synthetic.py`; the repository contains no recorded broadcast or camera
material, and contributions must not add any.

`python/adshut/tools/` has offline tools for a data directory: `replay` (detector over a
recorded day versus your presses), `sim` (what-if simulation of the learner), `mine`, `addtpl`,
`sheet`.

## Data, content and legal notes

This repository ships **code only**: no templates, fingerprints, recordings or channel lists.
Everything AdShut recognises is learned on each installation from its own viewer's presses.

Whoever runs a template server decides what it collects, how long it keeps it, whom it serves
and on what terms. The rules on viewing data and on derived data such as fingerprints differ
between countries. If you run a server for other people, you are the one responsible for
following your local law and for telling them your terms.

AdShut changes only the volume of the TV it is installed on. It does not alter, skip, record or
redistribute any broadcast.

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Commits must be
signed off (`git commit -s`) under the [Developer Certificate of Origin](https://developercertificate.org/). Please:

- keep the privacy guarantees above: no change may send camera pictures or audio off the device,
  or add any data collection outside the template server protocol;
- change the Java and Python cores together and keep `./tests.sh` passing;
- never commit recordings, fingerprints, templates or screenshots of broadcasts.

## License

Copyright (C) 2026 Alpar Sandor.

AdShut is free software:

- the template server (`python/adshut/server.py`) is licensed under the
  [GNU Affero General Public License v3.0 or later](LICENSES/AGPL-3.0-or-later.txt), so anyone
  who offers a modified server to others must share their changes;
- everything else is licensed under the
  [GNU General Public License v3.0 or later](LICENSE).

Each source file carries an `SPDX-License-Identifier` line. There is NO WARRANTY, to the extent
permitted by law.
