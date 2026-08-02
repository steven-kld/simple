"""Paths, defaults and X-session bootstrap.

Standard library only, deliberately: the CLI imports this *before* importing
anything that touches X, so DISPLAY and XAUTHORITY are already set by the time
mss and pyautogui load.
"""

import os
import re
from pathlib import Path

DEFAULT_DISPLAY = ":0"

# Fraction of changed tiles above which a step escalates. Calibrate per machine
# with npc-calibrate; this default is only a starting point.
DEFAULT_THRESHOLD = 0.05
DEFAULT_FINAL_WAIT = 2.0

# Comparison grid: 480x270 greyscale, 16x9 tiles of 30x30 px.
GRID_COLS = 16
GRID_ROWS = 9
TILE = 30
SMALL_W = GRID_COLS * TILE
SMALL_H = GRID_ROWS * TILE

# Per-tile mean absolute difference, 0-255, above which the tile counts changed.
TILE_MAD = 12

OPERATOR_LOCK = "/tmp/npc-operator-present"

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


def run_lock():
    return home() / "run.lock"


def scenario_path(name):
    return home() / "scenarios" / f"{name}.json"


def refs_dir(name):
    return home() / "refs" / name


def meta_path(name):
    return refs_dir(name) / "meta.json"


def log_path(name):
    return home() / "logs" / f"{name}.log"


def ensure_dirs(name):
    for d in (home() / "scenarios", refs_dir(name), home() / "logs"):
        d.mkdir(parents=True, exist_ok=True)


def _xauthority_candidates():
    uid = os.getuid()
    return [
        Path.home() / ".Xauthority",
        Path(f"/run/user/{uid}/gdm/Xauthority"),
        Path(f"/run/user/{uid}/Xauthority"),
    ]


def bootstrap_display(display=None, xauthority=None):
    """Point this process at the desktop's X session.

    A non-interactive SSH command inherits neither, so both are set here before
    any X client library is imported.
    """
    os.environ["DISPLAY"] = display or os.environ.get("NPC_DISPLAY") or DEFAULT_DISPLAY

    xauth = xauthority or os.environ.get("NPC_XAUTHORITY")
    if not xauth:
        for candidate in _xauthority_candidates():
            if candidate.exists():
                xauth = str(candidate)
                break
    if xauth:
        os.environ["XAUTHORITY"] = xauth
    return os.environ["DISPLAY"], os.environ.get("XAUTHORITY")
