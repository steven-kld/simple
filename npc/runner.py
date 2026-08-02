"""The loop: click two buttons forever, and wake a human when the screen stops
looking boring.

The reference is the *boring* state - the "all appointments are currently
booked" response. Matching it means nothing has happened, keep going. Deviating
from it means something happened, wake the operator. That inversion is the
whole design.
"""

import fcntl
import json
import os
import time
from datetime import datetime, timezone

from . import config, mouse, notify, screen

CLICKING = "clicking"
STOPPED = "stopped"

# Why the loop stopped clicking. Both are loud; a dead session must be as loud
# as a found slot, because a silent failure is the one that actually hurts.
CHANGED = "changed"
SESSION = "session"

# RustDesk opens its session window fullscreen, and that state defeats the two
# things the whole design rests on: openbox ignores `xdotool windowsize` while
# it is set, so a window that looks pinned is not, and Flutter under Xvfb
# reports wrong display metrics in it (flutter#162801). Clear it before
# recording anything: `wmctrl -i -r <id> -b remove,fullscreen`.
FULLSCREEN_WARNING = (
    "the session window is fullscreen; clear it with "
    "`wmctrl -i -r <id> -b remove,fullscreen` and pin the geometry, or "
    "xdotool will silently refuse to resize it"
)


class Abort(Exception):
    """Carries the JSON object the CLI will print before exiting."""

    def __init__(self, payload):
        super().__init__(payload.get("message", payload.get("status")))
        self.payload = payload


def _error(message):
    return Abort({"status": "error", "message": str(message)})


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --- the scenario -------------------------------------------------------


def _point(data, field, origin):
    point = data.get(field)
    if not isinstance(point, dict):
        raise _error(f"{origin}: '{field}' must be an object with x_pct and y_pct")
    out = {}
    for axis in ("x_pct", "y_pct"):
        value = point.get(axis)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _error(f"{origin}: {field}.{axis} must be a number")
        if not 0.0 <= value <= 1.0:
            raise _error(f"{origin}: {field}.{axis} must be a fraction of the content rectangle, 0-1")
        out[axis] = float(value)
    return out


def parse_plan(text, origin):
    """Two coordinates as fractions of the content rectangle, and two waits.

    Coordinates are stored as percentages, not pixels, so a change in the scale
    RustDesk renders at does not silently move every target.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _error(f"{origin}: not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise _error(f"{origin}: expected a JSON object")

    plan = {"book": _point(data, "book", origin), "ok": _point(data, "ok", origin)}
    for field, default in (
        ("threshold", config.DEFAULT_THRESHOLD),
        ("book_wait", config.DEFAULT_BOOK_WAIT),
        ("ok_wait", config.DEFAULT_OK_WAIT),
        ("recheck_seconds", config.RECHECK_SECONDS),
        ("watch_interval", config.WATCH_INTERVAL),
    ):
        value = data.get(field, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise _error(f"{origin}: {field} must be a non-negative number")
        plan[field] = float(value)

    debounce = data.get("debounce", config.DEBOUNCE)
    if not isinstance(debounce, int) or isinstance(debounce, bool) or debounce < 1:
        raise _error(f"{origin}: debounce must be an integer of at least 1")
    plan["debounce"] = debounce
    return plan


# --- locks --------------------------------------------------------------


def operator_present():
    return config.operator_lock().exists()


class RunLock:
    """flock-based, so the kernel releases it even if the process is killed."""

    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise Abort({"status": "busy"})
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        return False


# --- log ----------------------------------------------------------------


def log(name, message):
    path = config.log_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(f"{now()} name={name} {message}\n")


# --- references ---------------------------------------------------------


def read_meta(name):
    path = config.meta_path(name)
    if not path.exists():
        raise _error(
            f"no references for '{name}' at {path}; record them with npc-setup first"
        )
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise _error(f"{path} is corrupt: {exc}")


def write_meta(name, meta):
    config.meta_path(name).write_text(json.dumps(meta, indent=2) + "\n")


def check_resolution(name, meta, frame):
    height, width = frame.shape[:2]
    if meta["screen"]["width"] != width or meta["screen"]["height"] != height:
        raise _error(
            f"the display is {width}x{height} but the references for '{name}' were "
            f"recorded at {meta['screen']['width']}x{meta['screen']['height']}; the "
            f"coordinates no longer mean anything. Restore the resolution, or delete "
            f"{config.refs_dir(name)} and record again."
        )


# --- setup --------------------------------------------------------------


def region_around(point, radius, rect):
    """A square of `radius` around a click, clipped to the content rectangle."""
    x, y = point
    x0 = max(rect.x, x - radius)
    y0 = max(rect.y, y - radius)
    x1 = min(rect.x + rect.w, x + radius)
    y1 = min(rect.y + rect.h, y + radius)
    return screen.Rect(x0, y0, x1 - x0, y1 - y0)


def record(name, book, ok, threshold=None, window_name=None, content=None,
           book_wait=None, ok_wait=None, force=False, region=None, around=None,
           radius=200):
    """Pin the window, find the content rectangle, store the boring reference.

    `book` and `ok` are absolute coordinates on the VM's virtual display - what
    `xdotool getmouselocation` reports while hovering the target inside the
    remote view. They are converted to fractions of the content rectangle here.
    """
    config.check_name(name)
    config.ensure_dirs(name)

    if config.boring_path(name).exists() and not force:
        raise _error(
            f"references for '{name}' already exist. They are never overwritten "
            f"automatically - a reference that heals itself drifts silently onto the "
            f"wrong screen. Delete {config.refs_dir(name)} deliberately, or pass --force."
        )

    wid, window = screen.session(window_name)
    if window is None:
        raise _error(
            f"no window matching {window_name or config.window_name()!r} on display "
            f"{os.environ.get('DISPLAY')}; connect RustDesk to the host first"
        )

    frame = screen.capture()
    height, width = frame.shape[:2]
    rect = content if content is not None else screen.content_rect(frame, window)

    for label, (x, y) in (("book", book), ("ok", ok)):
        if not (rect.x <= x < rect.x + rect.w and rect.y <= y < rect.y + rect.h):
            raise _error(
                f"the {label} coordinate ({x}, {y}) is outside the content rectangle "
                f"{rect}. Hover the target inside the remote view and read it with "
                f"`xdotool getmouselocation`, on this display and not the son's."
            )

    # What gets compared. The default is the whole content rectangle: narrowing
    # it raises sensitivity to a small dialog and ignores an ad in the corner,
    # but anything that happens outside it is invisible, including a slot.
    if around is not None:
        region = region_around(around, radius, rect)
    watch = rect if region is None else region
    if not (
        rect.x <= watch.x
        and rect.y <= watch.y
        and watch.x + watch.w <= rect.x + rect.w
        and watch.y + watch.h <= rect.y + rect.h
    ):
        raise _error(
            f"the watched region {watch} is not inside the content rectangle {rect}"
        )
    if watch.w < 60 or watch.h < 60:
        raise _error(f"the watched region {watch} is too small to compare meaningfully")

    plan = {
        "book": {
            "x_pct": round((book[0] - rect.x) / rect.w, 6),
            "y_pct": round((book[1] - rect.y) / rect.h, 6),
        },
        "ok": {
            "x_pct": round((ok[0] - rect.x) / rect.w, 6),
            "y_pct": round((ok[1] - rect.y) / rect.h, 6),
        },
        "threshold": threshold if threshold is not None else config.DEFAULT_THRESHOLD,
        "book_wait": book_wait if book_wait is not None else config.DEFAULT_BOOK_WAIT,
        "ok_wait": ok_wait if ok_wait is not None else config.DEFAULT_OK_WAIT,
    }
    meta = {
        "name": name,
        "display": os.environ.get("DISPLAY"),
        "screen": {"width": width, "height": height},
        "window": window.as_dict(),
        "content": rect.as_dict(),
        # Stored as fractions of the content rectangle, like the coordinates,
        # so a change in the rendered scale moves the region with everything else.
        "region": {
            "x_pct": round((watch.x - rect.x) / rect.w, 6),
            "y_pct": round((watch.y - rect.y) / rect.h, 6),
            "w_pct": round(watch.w / rect.w, 6),
            "h_pct": round(watch.h / rect.h, 6),
        },
        "window_name": window_name or config.window_name(),
        "recorded": now(),
    }

    # The reference is the watched region alone, which is what every later
    # comparison crops to. The client's own chrome is not part of the question.
    screen.save(config.boring_path(name), screen.crop(frame, watch))
    write_meta(name, meta)
    config.scenario_path(name).write_text(json.dumps(plan, indent=2) + "\n")
    log(
        name,
        f"status=recorded window={window} content={rect} watch={watch} "
        f"threshold={plan['threshold']}",
    )
    result = {
        "status": "ok",
        "name": name,
        "window": window.as_dict(),
        "content": rect.as_dict(),
        "watching": watch.as_dict(),
        "whole_content": watch == rect,
        # A region that does not contain OK is legal - the dialog can sit above
        # the button - but it is worth seeing before you walk away for a night.
        "region_contains_book": _inside(watch, book),
        "region_contains_ok": _inside(watch, ok),
        "letterboxed": window.as_dict() != rect.as_dict(),
        "plan": str(config.scenario_path(name)),
        "reference": str(config.boring_path(name)),
    }
    if screen.is_fullscreen(wid):
        # Not fatal - the coordinates just recorded are real. But the geometry
        # guard cannot protect what the window manager will not hold still.
        result["warning"] = FULLSCREEN_WARNING
    return result


def _inside(rect, point):
    x, y = point
    return rect.x <= x < rect.x + rect.w and rect.y <= y < rect.y + rect.h


def watched_region(meta):
    """The absolute rectangle the comparison crops to, from the recorded meta."""
    content = screen.rect_from(meta["content"])
    region = meta.get("region")
    if not region:
        return content  # recorded before regions existed: the whole content
    return screen.Rect(
        content.x + int(round(region["x_pct"] * content.w)),
        content.y + int(round(region["y_pct"] * content.h)),
        max(1, int(round(region["w_pct"] * content.w))),
        max(1, int(round(region["h_pct"] * content.h))),
    )


def inspect(window_name=None):
    """What the agent can see, without writing anything."""
    frame = screen.capture()
    height, width = frame.shape[:2]
    found = screen.windows(window_name)
    out = {
        "status": "ok",
        "display": os.environ.get("DISPLAY"),
        "screen": {"width": width, "height": height},
        "pattern": window_name or config.window_name(),
        "rustdesk_running": screen.process_alive(),
        "windows": [
            {"id": wid, **rect.as_dict(), "fullscreen": screen.is_fullscreen(wid)}
            for wid, rect in found
        ],
    }
    if found:
        wid, window = found[0]
        rect = screen.content_rect(frame, window)
        out["content"] = rect.as_dict()
        out["letterboxed"] = window.as_dict() != rect.as_dict()
        if screen.is_fullscreen(wid):
            out["warning"] = FULLSCREEN_WARNING
    return out


def calibrate(shots, interval, rect=None):
    """Measure the noise floor: changed-tile fraction between identical states."""
    frames = []
    for i in range(shots):
        if i:
            time.sleep(interval)
        frame = screen.capture()
        frames.append(screen.crop(frame, rect) if rect else frame)

    pairs = [
        screen.compare(frames[i], frames[j])[0]
        for i in range(len(frames))
        for j in range(i + 1, len(frames))
    ]
    worst = max(pairs)
    return {
        "shots": shots,
        "pairs": len(pairs),
        "max": round(worst, 4),
        "mean": round(sum(pairs) / len(pairs), 4),
        # Three times the noise floor, with a floor of its own so a still
        # screen does not produce a threshold that nothing can pass.
        "recommended_threshold": max(round(worst * 3, 3), 0.02),
    }


# --- the watcher --------------------------------------------------------


class Watcher:
    """click BOOK, wait, look; click OK and repeat while the screen is boring."""

    def __init__(self, name, plan, meta, boring, notifier=None, emit=None,
                 sleep=time.sleep):
        self.name = name
        self.plan = plan
        self.meta = meta
        self.boring = boring
        self.notifier = notifier or notify.Disabled()
        self.emit = emit or (lambda event: None)
        self.sleep = sleep

        self.content = screen.rect_from(meta["content"])
        # Clicks are placed against the content rectangle; only the comparison
        # is narrowed. The two are deliberately independent.
        self.region = watched_region(meta)
        self.window = screen.rect_from(meta["window"])
        self.window_name = meta.get("window_name") or config.window_name()

        self.state = CLICKING
        self.reason = None
        self.matches = 0
        self.cycles = 0
        self.clicks = 0
        self.paused = False

    # -- plumbing --

    def _event(self, event, **fields):
        payload = {"time": now(), "name": self.name, "event": event, **fields}
        self.emit(payload)
        return payload

    def _look(self):
        """(full frame, changed-tile fraction against the boring reference)."""
        frame = screen.capture()
        diff, _ = screen.compare(screen.crop(frame, self.region), self.boring)
        return frame, diff

    def _click(self, target):
        x = self.content.x + int(round(target["x_pct"] * self.content.w))
        y = self.content.y + int(round(target["y_pct"] * self.content.h))
        mouse.click(x, y)
        self.clicks += 1
        return x, y

    def _session_problem(self):
        """Why the remote session cannot be trusted right now, or None.

        Window presence and geometry are the only cheap signals; anything else
        a dropped session does shows up as a mismatch instead, and both are
        alerted, so the screenshot tells the operator which it was.
        """
        try:
            window = screen.session_window(self.window_name)
        except screen.WindowError as exc:
            return str(exc)
        if window is None:
            running = screen.process_alive()
            return (
                "the RustDesk session window is gone"
                + ("" if running else " and no rustdesk process is running")
            )
        if window != self.window:
            return (
                f"the RustDesk window is now {window}, but the references were "
                f"recorded with it at {self.window}"
            )
        return None

    # -- alerts --

    def _alert(self, reason, message, frame=None, diff=None):
        limit = self.plan["threshold"]
        detail = "" if diff is None else f" diff={diff:.3f} limit={limit:.3f}"
        log(self.name, f"status={reason} clicks={self.clicks}{detail} message={message}")

        headline = "SLOT?" if reason == CHANGED else "DEAD SESSION?"
        lines = [f"[{headline}] npc/{self.name}", message, now()]
        if diff is not None:
            lines.append(f"difference {diff:.3f}, threshold {limit:.3f}")
        lines.append(f"{self.clicks} clicks so far. Clicking has stopped.")
        sent = self.notifier.alert(
            "\n".join(lines), png=None if frame is None else screen.png_bytes(frame)
        )
        self.state = STOPPED
        self.reason = reason
        self.matches = 0
        self._event("stopped", reason=reason, message=message, diff=diff, telegram=sent)

    def _resume(self):
        message = (
            "the boring 'all booked' screen came back and stayed for "
            f"{self.plan['debounce']} checks. Clicking has resumed on its own - "
            "nothing needs doing."
        )
        log(self.name, f"status=resumed clicks={self.clicks}")
        sent = self.notifier.alert(f"[RESUMED] npc/{self.name}\n{message}\n{now()}")
        self.state = CLICKING
        self.reason = None
        self.matches = 0
        self._event("resumed", telegram=sent)

    # -- the two cycles --

    def click_cycle(self):
        problem = self._session_problem()
        if problem:
            return self._alert(SESSION, problem, frame=self._frame_or_none())

        self._click(self.plan["book"])
        self.sleep(self.plan["book_wait"])
        frame, diff = self._look()

        strikes = 1
        while diff > self.plan["threshold"] and strikes < self.plan["debounce"]:
            # One frame is not an event. Codec churn and reconnect artefacts
            # do not survive three checks a few seconds apart; a slot does.
            self._event("strike", n=strikes, diff=round(diff, 4))
            self.sleep(self.plan["recheck_seconds"])
            problem = self._session_problem()
            if problem:
                return self._alert(SESSION, problem, frame=frame)
            frame, diff = self._look()
            strikes += 1

        if diff > self.plan["threshold"]:
            return self._alert(
                CHANGED,
                "the screen no longer matches the 'all booked' response - a slot may "
                "be open. Take over now.",
                frame=frame,
                diff=diff,
            )

        self._click(self.plan["ok"])
        self.sleep(self.plan["ok_wait"])
        self._event("cycle", diff=round(diff, 4), clicks=self.clicks,
                    strikes=strikes if strikes > 1 else None)

    def watch_cycle(self):
        """Stopped, but not blind: re-compare once a minute and resume if it clears."""
        self.sleep(self.plan["watch_interval"])
        problem = self._session_problem()
        if problem:
            self.matches = 0
            if problem != self.reason and self.reason != SESSION:
                # A different failure while already stopped is worth one message.
                self._alert(SESSION, problem, frame=self._frame_or_none())
            else:
                self._event("waiting", reason=self.reason, message=problem)
            return

        frame, diff = self._look()
        if diff <= self.plan["threshold"]:
            self.matches += 1
            self._event("recovering", matches=self.matches, diff=round(diff, 4))
            if self.matches >= self.plan["debounce"]:
                self._resume()
            return
        self.matches = 0
        # While the operator is filling in the booking form the screen shows
        # that form, which does not match the boring reference - so the loop
        # cannot resume underneath them. No presence detection is needed.
        self._event("waiting", reason=self.reason, diff=round(diff, 4))

    def _frame_or_none(self):
        try:
            return screen.capture()
        except screen.DisplayError:
            return None

    # -- the loop --

    def run(self, max_cycles=None):
        self._event("started", state=self.state, content=self.content.as_dict(),
                    watching=self.region.as_dict(),
                    telegram=self.notifier.configured)
        done = 0
        while max_cycles is None or done < max_cycles:
            done += 1
            self.cycles += 1
            if operator_present():
                # The operator is on the VM's own display; two things driving
                # one cursor is exactly what this prevents.
                if not self.paused:
                    self.paused = True
                    self._event("paused", message="an operator is on the VM's display")
                self.sleep(self.plan["recheck_seconds"])
                continue
            if self.paused:
                self.paused = False
                self._event("unpaused")

            if self.state == CLICKING:
                self.click_cycle()
            else:
                self.watch_cycle()
        return {"status": "ok", "cycles": self.cycles, "clicks": self.clicks,
                "state": self.state}


def watch(name, threshold=None, notifier=None, emit=None, max_cycles=None,
          sleep=time.sleep):
    config.check_name(name)
    config.ensure_dirs(name)

    meta = read_meta(name)
    path = config.scenario_path(name)
    if not path.exists():
        raise _error(f"no plan at {path}; record one with npc-setup")
    plan = parse_plan(path.read_text(), str(path))
    if threshold is not None:
        plan["threshold"] = threshold

    boring_path = config.boring_path(name)
    if not boring_path.exists():
        raise _error(f"no reference at {boring_path}; record one with npc-setup")

    with RunLock(config.watch_lock()):
        frame = screen.capture()
        check_resolution(name, meta, frame)
        boring = screen.load(boring_path)
        watcher = Watcher(name, plan, meta, boring, notifier, emit, sleep)
        return watcher.run(max_cycles)
