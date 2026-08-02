"""Paths, defaults and X-session bootstrap.

Standard library only, deliberately: the CLI imports this *before* importing
anything that touches X, so DISPLAY is already set by the time mss and
pyautogui load.
"""

import os
import re
from pathlib import Path

# Xvfb, not a physical screen. The VM has no monitor and the automation must
# never move a real cursor.
DEFAULT_DISPLAY = ":99"

# Fraction of changed tiles above which the screen is no longer the boring
# "all booked" response. Calibrate per install with npc-calibrate; this default
# is only a starting point.
DEFAULT_THRESHOLD = 0.05

# Consecutive checks that must agree before anything is believed, in either
# direction. The picture arrives over a video codec whose compression varies
# with bandwidth, so an unchanged screen still churns pixels, and a reconnect
# or a momentary artefact differs wildly for one frame. Neither survives three
# checks a few seconds apart; a real slot does. This is the primary defence
# against false alarms.
DEBOUNCE = 3
RECHECK_SECONDS = 4.0

# Once stopped, re-compare this often. Slow on purpose: nothing is being
# clicked, and the operator may be filling in the booking form.
WATCH_INTERVAL = 60.0

DEFAULT_BOOK_WAIT = 3.0
DEFAULT_OK_WAIT = 1.5

# Comparison grid: 480x270 greyscale, 16x9 tiles of 30x30 px.
GRID_COLS = 16
GRID_ROWS = 9
TILE = 30
SMALL_W = GRID_COLS * TILE
SMALL_H = GRID_ROWS * TILE

# Per-tile mean absolute difference, 0-255, above which the tile counts changed.
TILE_MAD = 12

# Guards the VM's own virtual display, so a debugging VNC connection does not
# fight the automation for the cursor. Nothing to do with the son's machine.
OPERATOR_LOCK = "/tmp/npc-operator-present"

# The RustDesk Flutter client is multi-window: connecting spawns a second
# top-level window rather than filling the first. The main window is titled
# exactly "RustDesk", the remote session carries the peer id in front of it.
DEFAULT_WINDOW_NAME = r".+ - RustDesk$"

# Greyscale level below which a pixel counts as letterbox padding.
BLACK_LEVEL = 10

TELEGRAM_TOKEN_ENV = "NPC_TELEGRAM_TOKEN"
TELEGRAM_CHAT_ENV = "NPC_TELEGRAM_CHAT_ID"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ConfigError(Exception):
    pass


def check_name(name):
    if not _NAME_RE.match(name or ""):
        raise ConfigError(
            "scenario name must match [A-Za-z0-9][A-Za-z0-9._-]* (it is used as a path)"
        )
    return name


def home():
    return Path(os.environ.get("NPC_HOME", Path.home() / ".npc"))


def operator_lock():
    return Path(os.environ.get("NPC_OPERATOR_LOCK", OPERATOR_LOCK))


def watch_lock():
    return home() / "watch.lock"


def scenario_path(name):
    return home() / "scenarios" / f"{name}.json"


def refs_dir(name):
    return home() / "refs" / name


def boring_path(name):
    return refs_dir(name) / "boring.png"


def meta_path(name):
    return refs_dir(name) / "meta.json"


def log_path(name):
    return home() / "logs" / f"{name}.log"


def ensure_dirs(name):
    for d in (home() / "scenarios", refs_dir(name), home() / "logs"):
        d.mkdir(parents=True, exist_ok=True)


def window_name():
    return os.environ.get("NPC_WINDOW_NAME", DEFAULT_WINDOW_NAME)


def telegram_credentials():
    return (
        os.environ.get(TELEGRAM_TOKEN_ENV, "").strip(),
        os.environ.get(TELEGRAM_CHAT_ENV, "").strip(),
    )


def _xauthority_candidates():
    uid = os.getuid()
    return [
        Path.home() / ".Xauthority",
        Path(f"/run/user/{uid}/Xauthority"),
    ]


def bootstrap_display(display=None, xauthority=None):
    """Point this process at the virtual display.

    A systemd unit or a non-interactive SSH command inherits neither, so both
    are set here before any X client library is imported.

    Xvfb started without -auth needs no cookie, but python-xlib does not treat
    "no XAUTHORITY" as "no authority needed": it falls back to ~/.Xauthority
    and raises XauthError when that is absent, which under systemd it always
    is. Pointing it at an empty file is how you say "there is no cookie, and
    that is fine" - it warns and connects. Inheriting a real one from a
    desktop session is why this works interactively and not as a service.
    """
    os.environ["DISPLAY"] = display or os.environ.get("NPC_DISPLAY") or DEFAULT_DISPLAY

    xauth = xauthority or os.environ.get("NPC_XAUTHORITY") or os.environ.get("XAUTHORITY")
    if not xauth or not Path(xauth).exists():
        xauth = next((str(c) for c in _xauthority_candidates() if c.exists()), os.devnull)
    os.environ["XAUTHORITY"] = xauth
    return os.environ["DISPLAY"], os.environ["XAUTHORITY"]
