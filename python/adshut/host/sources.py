# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
"""Frame sources for the host: each yields (epoch seconds, JPEG bytes) while frames are wanted.

  mjpeg    multipart MJPEG over HTTP, e.g. the adshut TV app's /stream.mjpg
  command  any program writing a raw MJPEG stream (back-to-back JPEGs) to stdout:
           ffmpeg from a V4L2 webcam, rpicam-vid for the Pi camera module, GStreamer, ...
  v4l2     shortcut for command: ffmpeg reading a V4L2 device (copies the camera's own JPEGs
           when it offers MJPEG, else encodes them)
"""
import json
import subprocess
import time
import urllib.request


class MjpegSource:
    def __init__(self, url, status_url=None):
        self.url = url
        self.status_url = status_url

    def describe_error(self, err):
        """More detail for an error, if the source can tell (e.g. the TV app's camera status)."""
        if not self.status_url:
            return err
        try:
            with urllib.request.urlopen(self.status_url, timeout=5) as r:
                return err + " / app: " + (json.load(r).get("last_error") or "no error")
        except Exception:
            return err

    def frames(self, wanted):
        """Yields (t, jpeg) while wanted() is true; raises on stream errors."""
        with urllib.request.urlopen(self.url, timeout=15) as resp:
            while wanted():
                line = resp.readline()
                if not line:
                    raise IOError("stream ended")
                if not line.startswith(b"--frame"):
                    continue
                length = 0
                while True:
                    h = resp.readline().strip()
                    if not h:
                        break
                    if h.lower().startswith(b"content-length:"):
                        length = int(h.split(b":")[1])
                jpeg = resp.read(length)
                if len(jpeg) != length or not length:
                    raise IOError("short frame")
                yield time.time(), jpeg


def _die_with_parent():
    """Linux: the frame program gets SIGTERM when the host dies, even if it is killed outright."""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except Exception:
        pass


class CommandSource:
    """Runs a command and splits its stdout into JPEG frames (SOI ... EOI markers)."""

    def __init__(self, cmd):
        self.cmd = list(cmd)
        self.stderr_tail = ""

    def describe_error(self, err):
        return err + (" / " + self.stderr_tail if self.stderr_tail else "")

    def frames(self, wanted):
        p = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=_die_with_parent)
        buf = b""
        try:
            while wanted():
                chunk = p.stdout.read1(65536) if hasattr(p.stdout, "read1") else p.stdout.read(65536)
                if not chunk:
                    self.stderr_tail = p.stderr.read().decode(errors="replace").strip()[-300:]
                    raise IOError("frame command ended (exit %s)" % p.poll())
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        buf = buf[-1:]
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        buf = buf[start:]
                        break
                    yield time.time(), buf[start:end + 2]
                    buf = buf[end + 2:]
                if len(buf) > 8 * 1024 * 1024:          # no frame boundary in 8 MB: not MJPEG
                    raise IOError("frame command output is not an MJPEG stream")
        finally:
            p.kill()
            p.wait()


def v4l2_command(device="/dev/video0", size="640x360", fps=10, input_format="mjpeg"):
    """ffmpeg command for a V4L2 camera; MJPEG cameras are copied, others encoded at 5 fps."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-input_format", input_format,
           "-video_size", size, "-framerate", str(fps), "-i", device]
    if input_format == "mjpeg":
        return cmd + ["-c:v", "copy", "-f", "mjpeg", "pipe:1"]
    return cmd + ["-r", "5", "-c:v", "mjpeg", "-q:v", "4", "-f", "mjpeg", "pipe:1"]
