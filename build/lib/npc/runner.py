"""The run loop: record on the first run, verify on every later one."""

import fcntl
import hashlib
import json
import os
import time
from datetime import datetime, timezone

from . import config, mouse, screen


class Abort(Exception):
    """Carries the JSON object the agent will print and exit with."""

    def __init__(self, payload):
        super().__init__(payload.get("message", payload.get("status")))
        self.payload = payload


def _error(message, step=None):
    payload = {"status": "error", "message": str(message)}
    if step is not None:
        payload["step"] = step
    return Abort(payload)


# --- scenario -----------------------------------------------------------


def parse_steps(text, origin):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _error(f"{origin}: not valid JSON: {exc}")

    # A flat list is the format; the object form only exists to carry a
    # per-scenario threshold alongside it.
    threshold = None
    if isinstance(data, dict):
        threshold = data.get("threshold")
        data = data.get("steps")
    if not isinstance(data, list) or not data:
        raise _error(f"{origin}: expected a non-empty list of steps")

    for i, step in enumerate(data):
        if not isinstance(step, dict):
            raise _error(f"{origin}: step {i} is not an object")
        action = step.get("action")
        if action == "click":
            for axis in ("x", "y"):
                if not isinstance(step.get(axis), int) or isinstance(step[axis], bool):
                    raise _error(f"{origin}: step {i} click needs integer {axis}")
        elif action == "wait":
            seconds = step.get("seconds")
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
                raise _error(f"{origin}: step {i} wait needs numeric seconds")
            if seconds < 0:
                raise _error(f"{origin}: step {i} wait seconds must not be negative")
        else:
            raise _error(f"{origin}: step {i} action must be 'click' or 'wait'")

    if threshold is not None and not isinstance(threshold, (int, float)):
        raise _error(f"{origin}: threshold must be a number")
    return data, threshold


def _canonical(steps):
    return json.dumps(steps, sort_keys=True, separators=(",", ":"))


def _fingerprint(steps):
    return hashlib.sha256(_canonical(steps).encode()).hexdigest()


# --- locks --------------------------------------------------------------


def operator_present():
    return config.operator_lock().exists()


class RunLock:
    """flock-based, so the kernel releases it even if the SSH channel dies."""

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
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with path.open("a") as fh:
        fh.write(f"{stamp} name={name} {message}\n")


# --- references ---------------------------------------------------------


def _read_meta(name):
    path = config.meta_path(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise _error(f"{path} is corrupt: {exc}")


def _write_meta(name, meta):
    config.meta_path(name).write_text(json.dumps(meta, indent=2) + "\n")


def _check_resolution(name, meta, frame):
    height, width = frame.shape[:2]
    if meta["width"] != width or meta["height"] != height:
        raise _error(
            f"screen is {width}x{height} but references for '{name}' were recorded "
            f"at {meta['width']}x{meta['height']}; the coordinates no longer mean "
            f"anything. Restore the resolution, or delete {config.refs_dir(name)} "
            f"and record again."
        )


def _check_scenario_matches(name, steps, meta):
    if meta.get("fingerprint") != _fingerprint(steps):
        raise _error(
            f"the scenario differs from the one the references for '{name}' were "
            f"recorded against. Delete {config.refs_dir(name)} to record again, "
            f"or run the new scenario under a different --name."
        )


# --- the loop -----------------------------------------------------------


def run(name, steps, threshold=None, final_wait=None):
    config.check_name(name)
    config.ensure_dirs(name)

    if operator_present():
        return {"status": "locked"}

    with RunLock(config.run_lock()):
        return _run_locked(name, steps, threshold, final_wait)


def _run_locked(name, steps, threshold, final_wait):
    refs = config.refs_dir(name)
    meta = _read_meta(name)
    frame = screen.capture()
    height, width = frame.shape[:2]

    if meta is None:
        meta = {
            "name": name,
            "width": width,
            "height": height,
            "threshold": (
                threshold if threshold is not None else config.DEFAULT_THRESHOLD
            ),
            "fingerprint": _fingerprint(steps),
            "recorded": datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"
            ),
        }
        _write_meta(name, meta)
        config.scenario_path(name).write_text(json.dumps(steps, indent=2) + "\n")
    else:
        _check_resolution(name, meta, frame)
        _check_scenario_matches(name, steps, meta)

    limit = threshold if threshold is not None else meta["threshold"]
    wait_after = final_wait if final_wait is not None else config.DEFAULT_FINAL_WAIT

    for index, step in enumerate(steps):
        if operator_present():
            return {"status": "locked"}

        if step["action"] == "wait":
            time.sleep(step["seconds"])
            continue

        x, y = step["x"], step["y"]
        if not (0 <= x < width and 0 <= y < height):
            raise _error(f"click at ({x}, {y}) is outside the {width}x{height} screen", index)

        frame = screen.capture()
        reference = refs / f"step-{index}.png"

        if not reference.exists():
            # First run: record the state in which this coordinate was known good.
            screen.save(reference, frame)
        else:
            mismatch = _verify(name, frame, reference, limit, index)
            if mismatch:
                return mismatch

        mouse.click(x, y)

    time.sleep(wait_after)

    # Each step's check validates the *previous* step's outcome, so the last
    # action needs its own or a silently failed final click still reports ok.
    frame = screen.capture()
    final = refs / "final.png"
    if not final.exists():
        screen.save(final, frame)
    else:
        mismatch = _verify(name, frame, final, limit, len(steps))
        if mismatch:
            return mismatch

    return {"status": "ok", "steps": len(steps)}


def _verify(name, frame, reference, limit, index):
    """None when the screen still matches the reference, else the mismatch payload."""
    diff, changed = screen.compare(frame, screen.load(reference))
    if diff <= limit:
        return None
    log(
        name,
        f"step={index} status=mismatch diff={diff:.3f} limit={limit:.3f} "
        f"tiles={int(changed.sum())}/{changed.size}",
    )
    return {
        "status": "mismatch",
        "step": index,
        "diff": round(diff, 4),
        "screenshot": screen.png_base64(frame),
    }


def calibrate(shots, interval):
    """Measure the noise floor: changed-tile fraction between identical states."""
    frames = []
    for i in range(shots):
        if i:
            time.sleep(interval)
        frames.append(screen.capture())

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
        # Three times the noise floor, with a floor of its own so a perfectly
        # still screen does not produce a threshold that nothing can pass.
        "recommended_threshold": max(round(worst * 3, 3), 0.02),
    }
