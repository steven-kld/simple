"""Entry points: npc-setup records, npc-watch runs the loop, npc-calibrate measures.

Nothing here imports an X client at module level: DISPLAY has to be set first,
and a display that cannot be reached must come back as {"status": "error"}
rather than a traceback.
"""

import argparse
import json
import os
import sys

from . import config

EXIT_CODES = {"ok": 0, "error": 1, "busy": 4}


def _emit(payload):
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return EXIT_CODES.get(payload.get("status"), 1)


def _display_args(parser):
    parser.add_argument("--display", help="X display, default $NPC_DISPLAY or :99")
    parser.add_argument(
        "--xauthority",
        help="X authority file, default $NPC_XAUTHORITY, or ~/.Xauthority / "
        "/run/user/$UID/Xauthority if either exists",
    )
    parser.add_argument(
        "--window-name",
        help=f"regex matching the RustDesk session window, default $NPC_WINDOW_NAME "
        f"or {config.DEFAULT_WINDOW_NAME!r}",
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


def _point(text, label):
    try:
        x, y = (int(part) for part in text.split(","))
    except ValueError:
        raise SystemExit(
            _emit({"status": "error", "message": f"--{label} wants X,Y in display pixels"})
        )
    return x, y


# --- npc-setup ----------------------------------------------------------


def setup(argv=None):
    parser = argparse.ArgumentParser(
        prog="npc-setup",
        description="Record the boring reference and the two coordinates, once.",
    )
    parser.add_argument("--name", help="scenario name; references are filed under it")
    parser.add_argument("--book", help="BOOK button as X,Y on this display (xdotool getmouselocation)")
    parser.add_argument("--ok", help="OK button as X,Y on this display")
    parser.add_argument("--threshold", type=float, help="changed-tile fraction that counts as a change")
    parser.add_argument("--book-wait", type=float, help=f"seconds after BOOK, default {config.DEFAULT_BOOK_WAIT}")
    parser.add_argument("--ok-wait", type=float, help=f"seconds after OK, default {config.DEFAULT_OK_WAIT}")
    parser.add_argument("--content", help="override the detected content rectangle: X,Y,W,H")
    parser.add_argument(
        "--region",
        help="compare only this rectangle, X,Y,W,H in display pixels; "
        "default is the whole content rectangle",
    )
    parser.add_argument(
        "--region-around",
        choices=("book", "ok"),
        help="compare only a square around that button (see --radius)",
    )
    parser.add_argument(
        "--radius",
        type=int,
        default=200,
        help="half-width of --region-around, in pixels (default 200)",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing references")
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="print the windows, geometry and content rectangle, and write nothing",
    )
    _display_args(parser)
    args = parser.parse_args(argv)

    config.bootstrap_display(args.display, args.xauthority)
    runner = _load_runner()

    try:
        if args.inspect:
            return _emit(runner.inspect(args.window_name))

        for required in ("name", "book", "ok"):
            if not getattr(args, required):
                return _emit(
                    {"status": "error", "message": f"--{required} is required (or use --inspect)"}
                )
        config.check_name(args.name)

        content = None
        if args.content:
            try:
                content = runner.screen.Rect(*(int(v) for v in args.content.split(",")))
            except (TypeError, ValueError):
                return _emit({"status": "error", "message": "--content wants X,Y,W,H"})

        book, ok = _point(args.book, "book"), _point(args.ok, "ok")

        if args.region and args.region_around:
            return _emit(
                {"status": "error", "message": "--region and --region-around are exclusive"}
            )
        region = None
        if args.region:
            try:
                region = runner.screen.Rect(*(int(v) for v in args.region.split(",")))
            except (TypeError, ValueError):
                return _emit({"status": "error", "message": "--region wants X,Y,W,H"})

        return _emit(
            runner.record(
                args.name,
                book,
                ok,
                threshold=args.threshold,
                window_name=args.window_name,
                content=content,
                book_wait=args.book_wait,
                ok_wait=args.ok_wait,
                force=args.force,
                region=region,
                around=(book if args.region_around == "book" else ok)
                if args.region_around
                else None,
                radius=args.radius,
            )
        )
    except Exception as exc:
        return _emit(_failure(runner, args.name, exc))


# --- npc-watch ----------------------------------------------------------


def watch(argv=None):
    parser = argparse.ArgumentParser(
        prog="npc-watch",
        description="Click BOOK and OK until the screen stops looking boring.",
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--threshold", type=float, help="overrides the recorded threshold")
    parser.add_argument("--cycles", type=int, help="stop after this many cycles (testing)")
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="run without notifications; the loop is then only as loud as its log",
    )
    _display_args(parser)
    args = parser.parse_args(argv)

    try:
        config.check_name(args.name)
    except config.ConfigError as exc:
        return _emit({"status": "error", "message": str(exc)})

    token, chat_id = config.telegram_credentials()
    if not args.no_telegram and not (token and chat_id):
        return _emit(
            {
                "status": "error",
                "message": (
                    f"set {config.TELEGRAM_TOKEN_ENV} and {config.TELEGRAM_CHAT_ENV}, or "
                    f"pass --no-telegram. The loop's whole output is a message to a human; "
                    f"running it unable to send one is the silent failure this guards against."
                ),
            }
        )

    config.bootstrap_display(args.display, args.xauthority)
    runner = _load_runner()

    from . import notify

    notifier = notify.from_env(
        (token, chat_id),
        log=lambda message: runner.log(args.name, message),
        enabled=not args.no_telegram,
    )

    try:
        result = runner.watch(
            args.name,
            threshold=args.threshold,
            notifier=notifier,
            emit=_line,
            max_cycles=args.cycles,
        )
    except KeyboardInterrupt:
        return _emit({"status": "ok", "message": "stopped by hand"})
    except runner.mouse.FailSafe:
        message = "the failsafe corner was hit; the run was aborted by hand"
        runner.log(args.name, f"status=failsafe message={message}")
        notifier.alert(f"[STOPPED] npc/{args.name}\n{message}")
        return _emit({"status": "error", "message": message})
    except Exception as exc:
        payload = _failure(runner, args.name, exc)
        if payload["status"] == "error":
            # Dying quietly is the failure mode that costs everything.
            notifier.alert(f"[STOPPED] npc/{args.name}\n{payload['message']}\n{runner.now()}")
        return _emit(payload)
    return _emit(result)


def _line(event):
    json.dump(event, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


# --- npc-calibrate ------------------------------------------------------


def calibrate(argv=None):
    parser = argparse.ArgumentParser(
        prog="npc-calibrate",
        description="Measure the noise floor of a settled screen and suggest a threshold.",
    )
    parser.add_argument(
        "--name",
        help="measure inside this scenario's watched region rather than the whole display",
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
        rect = None
        if args.name:
            config.check_name(args.name)
            # Calibrate on exactly what the loop compares, or the noise floor
            # is measured somewhere the watcher never looks.
            rect = runner.watched_region(runner.read_meta(args.name))
        result = runner.calibrate(args.shots, args.interval, rect)
    except Exception as exc:
        return _emit(_failure(runner, args.name, exc))
    result["status"] = "ok"
    return _emit(result)


# --- shared -------------------------------------------------------------


def _failure(runner, name, exc):
    if isinstance(exc, runner.Abort):
        payload = exc.payload
    else:
        payload = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    if payload["status"] == "error" and name:
        try:
            runner.log(name, f"status=error message={payload['message']}")
        except Exception:
            pass
    return payload


def main_setup():
    sys.exit(setup())


def main_watch():
    sys.exit(watch())


def main_calibrate():
    sys.exit(calibrate())
