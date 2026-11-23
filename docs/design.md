# AdShut design notes

How the parts work and why. The binary formats and the behaviour both implementations must
share are specified in [../spec/README.md](../spec/README.md).

## Screen tracking

The screen corners are found automatically (`python/adshut/screen.py`,
`android/src/adshut/core/ScreenFinder.java`, `ScreenTracker.java`), so the camera needs no
manual calibration and may be moved.

- A map of where fine picture detail changes from frame to frame is built over a window of
  frames. Only the TV picture does that: walls lit by the TV flicker smoothly and cancel out in
  the high-pass.
- The map is thresholded between its lower quartile (background) and top decile (picture),
  cleaned by a majority filter, holes are filled, and the largest blob is kept. A Hough vote over
  its outline gives the four edges, polished by least squares and inset by 2 px.
- `moved`: three consecutive measurements away from the current corners that agree with each
  other switch the calibration (a few minutes after a move).
- `refined`: the median of eight measurements near the current corners replaces them if it
  differs by more than about 2 px.
- The black bars of a letterboxed film look like a smaller screen and are ignored.

Every change is a `calibration` event with the old and new corners.

### Camera setup screen (TV app)

Opening AdShut from the launcher fills the screen with a slowly alternating mid-grey
checkerboard (one change per second). The TV screen is then the only thing changing in the
camera's view, and its outline is measured every 4 s while the camera is aimed. A panel shows
the camera view with the measured outline, readouts (screen width in the picture, viewing
angles, brightness, overexposure) and plain-language hints. Remote: OK saves the corners
(median of the last three measurements), LEFT/RIGHT shows the straightened view, DOWN or MENU
opens the settings, BACK leaves. The live loop pauses while the setup screen is open.

## Fingerprints and features

The screen area is warped to 320 × 180 and cut into 16 × 9 zones of 20 × 20 pixels; each zone
keeps its mean Y, Cb and Cr: 432 bytes per frame, 5 frames per second. Raw values depend on
camera exposure and white balance, so frames are compared through features that relate each
zone to the rest of its frame:

- luma: zone Y minus the frame's mean Y, divided by the frame's Y spread (at least 6 levels);
- chroma: zone Cb, Cr minus the frame's means, on a fixed scale (1/12 per level).

The feature vector is L2-normalised; two frames match when their vectors correlate. Frames with
little spatial structure (black, a single colour) carry little information and get a low
weight (`std / 12`, clipped to 0..1).

## Detector

```
not in break --mute marker ("start")--> in break --unmute marker ("end")--> not in break
```

- **Matching**: each template (a few seconds of fingerprints) is scored at the alignments ending
  on the newest frame or the one before, with ±1 frame slack per template frame. The score is
  the mean correlation over the best 60 % of the template's frames (an ident's first second is
  often a wipe); featureless frames count as −1. Default threshold 0.8, learned templates 0.85.
  Genuine repeats score 0.9–1.0 through a camera, unrelated pictures below about 0.65. One
  occurrence of a template fires once (15 s cooldown). The Java core caches dot products, so a
  new frame costs one correlation per template frame.
- **Settling**: an unmute marker is ignored in the first 10 s of a break.
- **Safety**: a break the detector muted is unmuted after 8 minutes, on a channel change, when
  Live TV is left, or when the picture is lost for 10 s. If the viewer presses mute or unmute,
  the detector follows; the safety rules never undo the viewer's own mute.
- **Back-off**: the TV app stops processing while Live TV is not in front or a menu or the
  programme guide is shown, so the TV stays responsive.
- Mutes the detector caused are logged with `by_detector: true`, every match and decision as a
  `detector` event. In shadow mode decisions are only logged and toasted, marked "(test)".

## Learning (`python/adshut/learn.py`)

Runs on the template server every few minutes, once per TV, all TVs sharing one template
library. The labels are the viewer's own mute and unmute presses.

- **Exact templates** (`corr.py`): the 40 s before a mute (20 s before an unmute) are correlated
  frame by frame with the last 14 days of that TV's fingerprints, leaving out the press's own
  break. Material that aired before shows up as diagonal runs that end exactly where the
  airings stop agreeing, so idents and cards come out at their true length (capped at 10 s).
  For a mute, every recurring moving segment is a candidate. For an unmute, it is the last one
  plus the still cards right after it, extended backwards while its airings contradict it.
  Still cards alone never become markers.
- **Checks against the viewer's history**: a mute candidate is dropped if it ever aired shortly
  before an unmute, or while the sound was on with no mute following (programme). An unmute
  candidate is dropped if it aired shortly before a mute or deep inside a muted break (an ad).
  Muted spans separated by under 60 s of sound count as one break.
- **Validation**: every run re-checks all enabled templates against the whole recording with
  the same rules and disables those that aired where they should not. This matters because
  channels reuse the same animation at different places (a show's intro and outro, station
  cards that air both inside and at the end of a break).
- **First sightings**: when nothing before a press has aired before, the 8 s before it are cut
  into overlapping 4 s provisional pieces and the press stays pending for 14 days; once its
  material recurs, exact templates replace the pieces.
- **Mined templates** (optional, `learn.py --mine`): after each muted break of at least 90 s ends,
  its interior (minus 10 s at the start and 60 s at the end, where presses lag the picture) is
  correlated against the history. Moving material that also aired in another break, and passes
  the mute-candidate checks, becomes a "mined" start template (at most 5 s, threshold 0.9). Mined
  material only ever mutes: inside a break it does nothing, and it catches breaks that began
  unnoticed or sound that came back too early. It never becomes an unmute marker, which
  mid-break material would make dangerous. At most 25 per channel stay enabled (most airings in
  breaks first), because every template costs the TV time on every frame.
- **Feedback**: a detector action reversed within 30 s is a strike for the template that caused
  it; one that stands is a success. A template with more strikes than successes is disabled.

A learner run publishes the library index once, at its end, so TVs never download templates
that validation or the cap have not yet checked. `learn.py --history DIR` adds older recordings
of the same TV (for example from a host it replaced) as history. On a server, `learn-all.sh`
passes `ADSHUT_LEARN_ARGS` to every run and a TV's `DATA/tvs/<id>/learn.args` to that TV's.

Templates live in `DATA/templates/` (`index.json` + `.npz`). An entry can be switched off with
`"enabled": false`; hosts pick up changes within 30 s.

## Template server (`python/adshut/server.py`)

Python standard library only, port 8766. Every request needs `Authorization: Bearer <token>`;
tokens are per TV (`python3 -m adshut.server --add-tv ID` prints one; only its SHA-256 is
stored in `DATA/tokens.json`).

| Request | Purpose |
|---|---|
| `GET /v1/templates[?channel=NAME]` | enabled templates `{version, templates:[{name, kind, threshold, frames, channel}]}`; ETag = version |
| `GET /v1/templates/NAME.zones` | a template's fingerprints: frames × 432 bytes |
| `GET /v1/sync/TV` | resume points `{"fp": {"YYYY-MM-DD": bytes}, "events": bytes}` |
| `PUT /v1/sync/TV/fp/YYYY-MM-DD?offset=N` | append fingerprint records (440 bytes each) at N |
| `PUT /v1/sync/TV/events?offset=N` | append event lines at N |

Uploads are resumable: data already stored is skipped, so a client simply retries after a
failure. A TV may write only its own data, kept in `DATA/tvs/<id>/`. Nothing else is uploaded:
no pictures, no audio.

### Recording viewer (`/ui/`, `python/adshut/viewer.py`, `python/adshut/web/viewer.html`)

A web page served by the template server for inspecting what a TV recorded. It needs an admin
token (`python3 -m adshut.server --add-admin NAME`, kept hashed in `DATA/admins.json`), which can
read every TV's data; TV tokens cannot. Open `http://SERVER:8766/` and paste the token.

- **Client and programme selector:** a client is a TV (plus the older recordings its learner reads
  through `--history`). A programme is a recorded stretch on one channel; gaps of up to 5 minutes
  (menus, the guide, another app) do not split it. The list shows each programme's breaks
  (detector/user mutes) and issues, filtered by day and channel.
- **Monitor:** the fingerprint at the cursor drawn as a 16×9 picture (smoothed, or as the zone
  grid), with time, channel, mute state and the app in front.
- **Timeline** (DVR style; wheel zooms, drag pans, click seeks): thumbnails, recording coverage,
  TV power and app in front, channel, muted spans (red = detector, orange = user; the end mark
  shows who unmuted), detector hits and actions, issues, the best start/end template score
  (views up to 45 minutes; computed on demand), brightness and motion, and where templates were cut.
- **Issues** are derived from the event log: missed opening (the user muted), false mute (the user
  unmuted within 60 s of a detector mute), missed closing (the user had to unmute, or the 8-minute
  failsafe did), wrong unmute (the user re-muted within 30 s of a detector unmute).
- Keys: Space play/pause, ←/→ frame, Shift+←/→ 10 s, J/L previous/next event, `,`/`.` previous/next
  issue, +/− zoom, F fit programme.

**Template review** (`/ui/templates`, the *Templates* button in the viewer's banner;
`python/adshut/web/templates.html`): every template in the library as a card with its frames
(animated on hover), kind, origin, threshold, successes and strikes, recurrences and what the
detector logs say it did (hits, mutes, unmutes, last hit). Filters: channel, kind, origin, state
(enabled, disabled, reviewed, with strikes, never hit), search; sorts by cut time, hits, strikes,
recurrences or last hit. The detail panel plays the template, links to the moment it was cut from
(and its break mark) in the recording viewer, lists every detector log line naming it, and takes
an admin's decision: **Disable**, **Enable** or **Clear review**. Decisions are kept in
`DATA/templates/review.json` (with who, when and an optional note) and are applied whenever the
index is loaded or saved, so they override the learner, survive a learner run that was already
in progress, and reach the TVs with their next library check. Clearing a decision leaves the
template as it is and hands it back to the learner. New templates record the client they were
learned from (`client`); for older ones the page finds the client that recorded that moment.
Double-clicking a template on the viewer's Templates track opens it here.

The fingerprints are what the TV uploads anyway (16×9 colour averages), so the viewer shows no
more than the server already holds.

## TV app HTTP interface (port 8765)

| URL | Returns |
|---|---|
| `/state` | power, foreground app, mute, volume, channel id/number/name, missing permissions (JSON) |
| `/events?since=N&wait=S` | state changes after event N; waits up to S s (max 55) for one |
| `/mute?on=1` / `on=0` | mutes / unmutes; optional `&note=` is shown as a toast |
| `/toast?text=` | shows a toast |
| `/config[?key=value…]` | shows or changes settings (server, tv, token, mode, toasts, corners) |
| `/live`, `/status` | live loop and camera status (JSON) |
| `/shot.jpg`, `/stream.mjpg` | camera pictures, **only** if "share the camera picture" is ticked on the TV's settings screen; otherwise 403 |

The interface has no authentication: anyone on the local network can read the TV's state,
mute it or change its server settings. Run it only on a network you trust.

On boot, Android 11 gives a foreground service camera access only when it was started while its
app was visible, so the boot receiver opens an invisible activity for a moment, which starts the
service (this needs the `SYSTEM_ALERT_WINDOW` grant).

## Python host adapters

`python3 -m adshut.host --config FILE` (options in `python/adshut/host/__main__.py`):

- TV: `androidtv-app` (the TV app over HTTP) or `command` (any commands for mute/unmute or a mute
  toggle, toast and power query: IR via `ir-ctl`/`irsend`, HDMI-CEC via `cec-client`). With a
  toggle-only TV the host keeps the mute state it believes the TV is in. An IR receiver's key
  events correct it and serve as labels, and the host's own transmissions are ignored for 1.5 s.
- Camera: `v4l2` (webcam via ffmpeg), `command` (any program writing MJPEG to stdout, e.g.
  `rpicam-vid`), or `mjpeg` over HTTP.
- A non-smart TV does not say what channel it shows, so its templates are kept under one label
  per TV.
