"""Entry points. Prints exactly one JSON object to stdout, then exits.

Nothing here imports an X client at module level: DISPLAY and XAUTHORITY have
to be set first, and a display that cannot be reached must come back as
{"status": "error"} rather than a traceback.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from . import config

EXIT_CODES = {"ok": 0, "error": 1, "mismatch": 2, "locked": 3, "busy": 4}


def _emit(payload):
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return EXIT_CODES.get(payload.get("status"), 1)


def _display_args(parser):
    parser.add_argument("--display", help="X display, default $NPC_DISPLAY or :0")
    parser.add_argument(
        "--xauthority",
        help="X authority file, default $NPC_XAUTHORITY or the first of "
        "~/.Xauthority, /run/user/$UID/gdm/Xauthority, /run/user/$UID/Xauthority",
    )


def _load_runner():
    """Import the X-touching modules, turning any failure into an error payload.

    SystemExit is caught alongside Exception on purpose: pyautogui's mouseinfo
    dependency calls sys.exit() when tkinter is missing, and that would
    otherwise leave stdout empty in breach of the one-JSON-object contract.
    """
    try:
        from . import runner
    except (Exception, SystemExit) as exc:  # pyautogui connects to X at import time
        detail = str(exc).strip() or type(exc).__name__
        raise SystemExit(
            _emit(
                {
                    "status": "error",
                    "message": (
                        f"the agent could not start: {detail} "
                        f"(DISPLAY={os.environ.get('DISPLAY')!r}, "
                        f"XAUTHORITY={os.environ.get('XAUTHORITY')!r})"
                    ),
                }
            )
        )
    return runner


def _read_scenario(name, source):
    if source == "-":
        return sys.stdin.read(), "stdin"
    path = config.scenario_path(name) if source is None else Path(source)
    if not path.exists():
        raise SystemExit(
            _emit({"status": "error", "message": f"no scenario at {path}"})
        )
    return path.read_text(), str(path)


def run(argv=None):
    parser = argparse.ArgumentParser(
        prog="npc-run", description="Run one scenario against the desktop browser."
    )
    parser.add_argument("--name", required=True, help="scenario name; references are filed under it")
    parser.add_argument(
        "--scenario",
        help="path to the scenario JSON, or - for stdin; "
        "defaults to ~/.npc/scenarios/<name>.json",
    )
    parser.add_argument("--threshold", type=float, help="changed-tile fraction that escalates")
    parser.add_argument(
        "--final-wait",
        type=float,
        help=f"seconds to settle before the end-state check (default {config.DEFAULT_FINAL_WAIT})",
    )
    _display_args(parser)
    args = parser.parse_args(argv)

    try:
        config.check_name(args.name)
    except config.ConfigError as exc:
        return _emit({"status": "error", "message": str(exc)})

    text, origin = _read_scenario(args.name, args.scenario)

    config.bootstrap_display(args.display, args.xauthority)
    runner = _load_runner()

    try:
        steps, file_threshold = runner.parse_steps(text, origin)
        threshold = args.threshold if args.threshold is not None else file_threshold
        return _emit(
            runner.run(args.name, steps, threshold, args.final_wait)
        )
    except runner.Abort as abort:
        payload = abort.payload
        if payload["status"] == "error":
            runner.log(
                args.name,
                f"step={payload.get('step', '-')} status=error message={payload['message']}",
            )
        return _emit(payload)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        try:
            runner.log(args.name, f"status=error message={message}")
        except Exception:
            pass
        return _emit({"status": "error", "message": message})


def calibrate(argv=None):
    parser = argparse.ArgumentParser(
        prog="npc-calibrate",
        description="Measure the noise floor of a settled screen and suggest a threshold.",
    )
    parser.add_argument("--shots", type=int, default=8)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between shots")
    _display_args(parser)
    args = parser.parse_args(argv)

    if args.shots < 2:
        return _emit({"status": "error", "message": "--shots must be at least 2"})

    config.bootstrap_display(args.display, args.xauthority)
    runner = _load_runner()
    try:
        result = runner.calibrate(args.shots, args.interval)
    except Exception as exc:
        return _emit({"status": "error", "message": f"{type(exc).__name__}: {exc}"})
    result["status"] = "ok"
    return _emit(result)


def main():
    sys.exit(run())


def main_calibrate():
    sys.exit(calibrate())
