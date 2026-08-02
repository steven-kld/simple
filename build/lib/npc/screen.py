"""Screen capture and tiled comparison.

Capture goes through mss (direct X11, ~5-15 ms) rather than pyautogui, whose
path may shell out to scrot or gnome-screenshot and fail confusingly when
neither is installed.
"""

import base64
import os

import cv2
import mss
import numpy as np

from . import config


class DisplayError(RuntimeError):
    pass


_sct = None


def _session():
    global _sct
    if _sct is None:
        try:
            _sct = mss.mss()
        except Exception as exc:  # no display, bad XAUTHORITY, Wayland
            raise DisplayError(
                f"cannot connect to display {os.environ.get('DISPLAY')!r} "
                f"with XAUTHORITY={os.environ.get('XAUTHORITY')!r}: {exc}"
            ) from exc
    return _sct


def capture():
    """One BGR frame of the primary monitor, at its native resolution."""
    sct = _session()
    monitors = sct.monitors
    if len(monitors) < 2:
        raise DisplayError("no monitor reported by X11")
    try:
        raw = sct.grab(monitors[1])  # [0] is the union of all screens
    except Exception as exc:
        raise DisplayError(f"screen capture failed: {exc}") from exc
    return np.ascontiguousarray(np.asarray(raw)[:, :, :3])


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

    The mask says *where* the change is: a couple of scattered tiles is cursor
    and clock noise, a contiguous cluster is a popup, most of the grid is the
    wrong page.
    """
    diff = np.abs(_tiles(frame_a) - _tiles(frame_b)).mean(axis=(2, 3))
    changed = diff > config.TILE_MAD
    return float(changed.mean()), changed


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
