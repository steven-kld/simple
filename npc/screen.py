"""The eye: what the agent looks at, and how it decides the screen changed.

Capture goes through mss (direct X11, ~5-15 ms) rather than pyautogui, whose
path may shell out to scrot or gnome-screenshot and fail confusingly when
neither is installed.

The target is a RustDesk client window on a virtual display, not a browser, so
this module also finds that window and works out which part of it is the remote
desktop rather than letterbox padding.
"""

import base64
import os
import subprocess
from typing import NamedTuple

import cv2
import mss
import numpy as np

from . import config


class DisplayError(RuntimeError):
    pass


class WindowError(RuntimeError):
    pass


class Rect(NamedTuple):
    x: int
    y: int
    w: int
    h: int

    def as_dict(self):
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    def __str__(self):
        return f"{self.w}x{self.h}+{self.x}+{self.y}"


def rect_from(data):
    return Rect(int(data["x"]), int(data["y"]), int(data["w"]), int(data["h"]))


# --- capture ------------------------------------------------------------

_sct = None


def _session():
    global _sct
    if _sct is None:
        try:
            _sct = mss.mss()
        except Exception as exc:  # no display, bad XAUTHORITY, Xvfb not running
            raise DisplayError(
                f"cannot connect to display {os.environ.get('DISPLAY')!r} "
                f"with XAUTHORITY={os.environ.get('XAUTHORITY')!r}: {exc}"
            ) from exc
    return _sct


def capture():
    """One BGR frame of the whole virtual display, at its native resolution."""
    sct = _session()
    monitors = sct.monitors
    if len(monitors) < 2:
        raise DisplayError("no monitor reported by X11")
    try:
        raw = sct.grab(monitors[1])  # [0] is the union of all screens
    except Exception as exc:
        raise DisplayError(f"screen capture failed: {exc}") from exc
    return np.ascontiguousarray(np.asarray(raw)[:, :, :3])


def crop(frame, rect):
    height, width = frame.shape[:2]
    x0 = max(0, min(rect.x, width))
    y0 = max(0, min(rect.y, height))
    x1 = max(x0, min(rect.x + rect.w, width))
    y1 = max(y0, min(rect.y + rect.h, height))
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise DisplayError(f"region {rect} does not overlap the {width}x{height} display")
    return np.ascontiguousarray(frame[y0:y1, x0:x1])


# --- the RustDesk window ------------------------------------------------


def _xdotool(*args):
    try:
        return subprocess.run(
            ["xdotool", *args], capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        raise WindowError("xdotool is not installed; the RustDesk window cannot be found")
    except subprocess.TimeoutExpired:
        raise WindowError(f"xdotool {' '.join(args)} timed out talking to the display")


def window_geometry(wid):
    proc = _xdotool("getwindowgeometry", "--shell", str(wid))
    if proc.returncode != 0:
        return None  # it closed between the search and now
    values = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    try:
        return Rect(
            int(values["X"]), int(values["Y"]), int(values["WIDTH"]), int(values["HEIGHT"])
        )
    except (KeyError, ValueError) as exc:
        raise WindowError(f"cannot parse xdotool geometry for window {wid}: {exc}")


def windows(pattern=None):
    """Every visible window whose title matches, largest first."""
    pattern = pattern or config.window_name()
    proc = _xdotool("search", "--onlyvisible", "--name", pattern)
    if proc.returncode not in (0, 1):  # 1 is simply "no match"
        raise WindowError(f"xdotool search failed: {proc.stderr.strip() or proc.returncode}")

    found = []
    for wid in proc.stdout.split():
        rect = window_geometry(wid)
        if rect is not None:
            found.append((wid, rect))
    found.sort(key=lambda item: item[1].w * item[1].h, reverse=True)
    return found


def session_window(pattern=None):
    """The remote session window, or None when the session is not up.

    Several windows can match while RustDesk is connecting, so take the
    largest: the remote desktop is always bigger than a dialog.
    """
    found = windows(pattern)
    return found[0][1] if found else None


def process_alive():
    """False only when we are sure no client is running."""
    try:
        proc = subprocess.run(
            ["pgrep", "-x", "rustdesk"], capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return True  # no pgrep, no opinion
    return proc.returncode == 0


# --- the content rectangle ----------------------------------------------


def content_rect(frame, window, black=None):
    """The remote desktop inside the client window, minus any letterbox bars.

    Percentages measured from the *window* edge are out by the width of the
    bar, which puts every coordinate wrong, so this is measured once at setup
    and recorded alongside the references.
    """
    black = config.BLACK_LEVEL if black is None else black
    grey = cv2.cvtColor(crop(frame, window), cv2.COLOR_BGR2GRAY)
    rows = grey.max(axis=1) > black
    cols = grey.max(axis=0) > black
    if not rows.any() or not cols.any():
        return window  # an entirely dark screen: trust the window

    top = int(np.argmax(rows))
    bottom = int(len(rows) - np.argmax(rows[::-1]))
    left = int(np.argmax(cols))
    right = int(len(cols) - np.argmax(cols[::-1]))
    trimmed = Rect(window.x + left, window.y + top, right - left, bottom - top)

    # A remote desktop that happens to be dark down one side would be trimmed
    # into nonsense. Refuse to guess that hard and keep the window instead.
    if trimmed.w < window.w * 0.5 or trimmed.h < window.h * 0.5:
        return window
    return trimmed


# --- comparison ---------------------------------------------------------


def _tiles(frame):
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(
        grey, (config.SMALL_W, config.SMALL_H), interpolation=cv2.INTER_AREA
    )
    return (
        small.reshape(config.GRID_ROWS, config.TILE, config.GRID_COLS, config.TILE)
        .swapaxes(1, 2)
        .astype(np.int16)
    )


def compare(frame_a, frame_b):
    """Fraction of changed tiles, plus the boolean 9x16 mask of which ones.

    Both frames are downsampled to a fixed 480x270 first, so a change in the
    scale RustDesk renders at is normalised away as long as the aspect ratio
    holds. The mask says *where* the change is: scattered tiles are codec
    churn, a contiguous cluster is a dialog, most of the grid is a new page.
    """
    diff = np.abs(_tiles(frame_a) - _tiles(frame_b)).mean(axis=(2, 3))
    changed = diff > config.TILE_MAD
    return float(changed.mean()), changed


# --- images on disk -----------------------------------------------------


def png_bytes(frame):
    ok, buf = cv2.imencode(".png", frame)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def png_base64(frame):
    return base64.b64encode(png_bytes(frame)).decode("ascii")


def save(path, frame):
    path.write_bytes(png_bytes(frame))


def load(path):
    frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError(f"cannot read reference image {path}")
    return frame
