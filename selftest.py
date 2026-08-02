#!/usr/bin/env python3
"""Self-check for the watch loop, the comparison, the geometry and the mouse path.

Runs anywhere: pyautogui is replaced by a stub before the agent imports it, and
the display, the RustDesk window and Telegram are all faked, so this needs no X
server, never moves a real cursor and never sends a message. What it cannot
cover is XTEST actuation and RustDesk itself - that is what the acceptance test
in the README is for.

    ./.venv/bin/python selftest.py
"""

import contextlib
import io
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import types

os.environ["NPC_HOME"] = tempfile.mkdtemp(prefix="npc-selftest-")
os.environ["NPC_OPERATOR_LOCK"] = os.path.join(os.environ["NPC_HOME"], "operator")
os.environ.pop("NPC_TELEGRAM_TOKEN", None)
os.environ.pop("NPC_TELEGRAM_CHAT_ID", None)

# --- stub pyautogui before npc.mouse imports it -----------------------------
stub = types.ModuleType("pyautogui")
stub.FAILSAFE = False
stub.PAUSE = 0
stub.FailSafeException = type("FailSafeException", (Exception,), {})
stub.position = lambda: (0, 0)
stub.moveTo = lambda x, y: None
stub.click = lambda: None
stub.size = lambda: (1920, 1080)
sys.modules["pyautogui"] = stub

import numpy as np  # noqa: E402

from npc import cli, config, mouse, notify, runner, screen  # noqa: E402

W, H = 1920, 1080
rng = np.random.default_rng(7)

# The client window sits on the virtual display with black letterbox bars down
# each side: the remote desktop is narrower than the window.
WINDOW = screen.Rect(160, 90, 1600, 900)
CONTENT = screen.Rect(260, 90, 1400, 900)


def display(content):
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    frame[CONTENT.y : CONTENT.y + CONTENT.h, CONTENT.x : CONTENT.x + CONTENT.w] = content
    return frame


# Blocky rather than per-pixel noise: a real remote desktop is mostly flat
# regions, and pure noise would not survive being downsampled twice.
REMOTE = np.repeat(
    np.repeat(rng.integers(40, 255, (CONTENT.h // 20, CONTENT.w // 20, 3), dtype=np.uint8), 20, 0),
    20,
    1,
)
BORING = display(REMOTE)

_slot = REMOTE.copy()
_slot[200:700, 300:1100] = 250  # a booking form where the "all booked" box was
SLOT = display(_slot)

_noise = REMOTE.copy()
_noise[10:40, 10:40] = 0  # a clock in the corner of the remote desktop
NOISY = display(_noise)

failures = []


def check(label, condition, detail=""):
    print(f"{'ok  ' if condition else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not condition:
        failures.append(label)


# --- comparison -------------------------------------------------------------

check("identical frames differ by nothing", screen.compare(BORING, BORING)[0] == 0.0)

frac, mask = screen.compare(BORING, NOISY)
check(
    "a clock in the corner stays under the threshold",
    frac <= config.DEFAULT_THRESHOLD,
    f"{frac:.4f}, {int(mask.sum())} tiles",
)

frac, mask = screen.compare(BORING, SLOT)
rows, cols = np.where(mask)
contiguous = (rows.max() - rows.min() + 1) * (cols.max() - cols.min() + 1) == mask.sum()
check(
    "a changed screen escalates, as one contiguous cluster",
    frac > config.DEFAULT_THRESHOLD and contiguous,
    f"{frac:.4f}, {int(mask.sum())} tiles",
)

import cv2  # noqa: E402

remote = screen.crop(BORING, CONTENT)
frac, _ = screen.compare(remote, cv2.resize(remote, (933, 600)))
check(
    "a change in the scale RustDesk renders at is normalised away",
    frac <= config.DEFAULT_THRESHOLD,
    f"{frac:.4f}",
)

# --- the content rectangle --------------------------------------------------

check(
    "letterbox bars are trimmed off the window",
    screen.content_rect(BORING, WINDOW) == CONTENT,
    str(screen.content_rect(BORING, WINDOW)),
)
check(
    "a window with no bars is its own content rectangle",
    screen.content_rect(display(REMOTE), CONTENT) == CONTENT,
)
dark = np.zeros((H, W, 3), dtype=np.uint8)
check(
    "an all-black screen is not trimmed into nonsense",
    screen.content_rect(dark, WINDOW) == WINDOW,
)
mostly_dark = np.zeros((H, W, 3), dtype=np.uint8)
mostly_dark[500:520, 900:920] = 255  # one bright speck
check(
    "a nearly black screen keeps the window rather than guessing",
    screen.content_rect(mostly_dark, WINDOW) == WINDOW,
)

# --- fake the display, the window and the mouse -----------------------------

state = {"frame": BORING, "window": WINDOW, "clicks": [], "frames": None}


def capture():
    if state["frames"]:
        return state["frames"].pop(0)
    return state["frame"]


screen.capture = capture
screen.session = lambda pattern=None: ("1", state["window"])
screen.session_window = lambda pattern=None: state["window"]
# No X server, so no window properties to read; the loop must not care.
screen.is_fullscreen = lambda wid: False
screen.process_alive = lambda: True
runner.mouse.click = lambda x, y: state["clicks"].append((x, y))


class Recorder:
    """Telegram, without Telegram."""

    configured = True

    def __init__(self):
        self.sent = []

    def alert(self, text, png=None, filename="screen.png"):
        self.sent.append((text, png))
        return True


# --- setup ------------------------------------------------------------------

BOOK = (960, 700)
OK = (860, 540)

result = runner.record("demo", BOOK, OK, threshold=0.04)
check(
    "setup finds the window and the content rectangle inside it",
    result["status"] == "ok"
    and result["content"] == CONTENT.as_dict()
    and result["letterboxed"],
    str(result.get("content")),
)

check(
    "a pinned window records without a fullscreen warning",
    "warning" not in result,
    str(result.get("warning")),
)

# RustDesk opens its session window fullscreen, and openbox then ignores every
# attempt to resize it - so the window looks pinned and is not. Silent, and
# only visible in xprop, which is exactly why it is worth saying out loud.
screen.is_fullscreen = lambda wid: True
fullscreen = runner.record("demo", BOOK, OK, threshold=0.04, force=True)
check(
    "a fullscreen session window is recorded but reported",
    fullscreen["status"] == "ok" and "wmctrl" in fullscreen.get("warning", ""),
    str(fullscreen.get("warning")),
)
screen.is_fullscreen = lambda wid: False

plan = runner.parse_plan(config.scenario_path("demo").read_text(), "plan")
check(
    "coordinates are stored as fractions of the content rectangle, not pixels",
    plan["book"] == {"x_pct": 0.5, "y_pct": round((700 - 90) / 900, 6)},
    str(plan["book"]),
)

try:
    runner.record("demo", BOOK, OK)
    check("references are never overwritten automatically", False)
except runner.Abort as abort:
    check(
        "references are never overwritten automatically",
        "never overwritten" in abort.payload["message"],
    )

try:
    runner.record("outside", (100, 700), OK, force=True)
    check("a coordinate outside the content rectangle is refused", False)
except runner.Abort as abort:
    check(
        "a coordinate outside the content rectangle is refused",
        "outside the content rectangle" in abort.payload["message"],
    )

# --- the watched region -----------------------------------------------------

# An ad in the corner of the remote desktop, far from either button.
AD = display(REMOTE.copy())
AD[40:240, 1100:1390] = 180

check(
    "watching the whole screen, an ad in the corner is a change",
    screen.compare(screen.crop(BORING, CONTENT), screen.crop(AD, CONTENT))[0] > 0.03,
    f"{screen.compare(screen.crop(BORING, CONTENT), screen.crop(AD, CONTENT))[0]:.4f}",
)

region = runner.record(
    "narrow", BOOK, OK, threshold=0.04, around=OK, radius=200
)["watching"]
check(
    "--region-around records a square around the button, clipped to the content",
    region == {"x": 660, "y": 340, "w": 400, "h": 400},
    str(region),
)

narrow_meta = runner.read_meta("narrow")
check(
    "the region is stored as fractions of the content rectangle",
    narrow_meta["region"] == {
        "x_pct": round(400 / 1400, 6), "y_pct": round(250 / 900, 6),
        "w_pct": round(400 / 1400, 6), "h_pct": round(400 / 900, 6),
    },
    str(narrow_meta["region"]),
)
check(
    "and converts back to the same pixels",
    runner.watched_region(narrow_meta) == screen.Rect(660, 340, 400, 400),
    str(runner.watched_region(narrow_meta)),
)
tight = runner.record("narrow2", BOOK, OK, around=OK, radius=100, force=True)
check(
    "setup says plainly which buttons the region covers",
    tight["region_contains_ok"] is True and tight["region_contains_book"] is False,
    f"ok={tight['region_contains_ok']} book={tight['region_contains_book']}",
)

narrow_plan = runner.parse_plan(config.scenario_path("narrow").read_text(), "plan")
narrow_boring = screen.load(config.boring_path("narrow"))


def narrow_watcher(notifier):
    state["clicks"].clear()
    state["frames"] = None
    return runner.Watcher(
        "narrow", dict(narrow_plan), narrow_meta, narrow_boring, notifier,
        sleep=lambda s: None
    )


phone = Recorder()
state["frame"] = AD
narrow_watcher(phone).run(max_cycles=2)
check(
    "watching a region, the same ad is ignored and the loop keeps clicking",
    state["clicks"] == [BOOK, OK] * 2 and phone.sent == [],
    f"{len(state['clicks'])} clicks",
)

phone = Recorder()
state["frame"] = SLOT
w = narrow_watcher(phone)
w.run(max_cycles=1)
check(
    "a change inside the region still stops the loop",
    w.state == runner.STOPPED and len(phone.sent) == 1,
)

# The cost of narrowing, stated out loud rather than discovered at 3am.
FAR = display(REMOTE.copy())
FAR[750:880, 200:900] = 90  # a slot list appearing at the bottom left
phone = Recorder()
state["frame"] = FAR
w = narrow_watcher(phone)
w.run(max_cycles=2)
check(
    "a change OUTSIDE the region is invisible - the documented cost of narrowing",
    w.state == runner.CLICKING and phone.sent == [],
)
state["frame"] = BORING

expect_region_error = lambda **kw: runner.record("bad", BOOK, OK, force=True, **kw)
try:
    expect_region_error(region=screen.Rect(0, 0, 400, 400))
    check("a region outside the content rectangle is refused", False)
except runner.Abort as abort:
    check(
        "a region outside the content rectangle is refused",
        "not inside the content rectangle" in abort.payload["message"],
    )
try:
    expect_region_error(region=screen.Rect(300, 300, 40, 40))
    check("a region too small to compare is refused", False)
except runner.Abort as abort:
    check("a region too small to compare is refused", "too small" in abort.payload["message"])

# --- the loop ---------------------------------------------------------------

meta = runner.read_meta("demo")
boring = screen.load(config.boring_path("demo"))


def watcher(notifier=None):
    state["clicks"].clear()
    state["frames"] = None
    return runner.Watcher(
        "demo", dict(plan), meta, boring, notifier or Recorder(), sleep=lambda s: None
    )


BOOK_PX, OK_PX = BOOK, OK

phone = Recorder()
w = watcher(phone)
w.run(max_cycles=4)
check(
    "the boring screen means click BOOK, click OK, and go round again",
    state["clicks"] == [BOOK_PX, OK_PX] * 4 and w.state == runner.CLICKING,
    f"{len(state['clicks'])} clicks",
)
check("nothing boring is worth a message", phone.sent == [])

check(
    "the click lands back on the pixel it was recorded at",
    state["clicks"][0] == BOOK,
    str(state["clicks"][0]),
)

# one bad frame is codec churn, not an event
phone = Recorder()
w = watcher(phone)
state["frames"] = [SLOT, BORING]
w.run(max_cycles=1)
check(
    "a single mismatch is absorbed and the loop keeps clicking",
    state["clicks"] == [BOOK_PX, OK_PX] and w.state == runner.CLICKING and phone.sent == [],
    str(state["clicks"]),
)

# two is still not three
phone = Recorder()
w = watcher(phone)
state["frames"] = [SLOT, SLOT, BORING]
w.run(max_cycles=1)
check(
    "two consecutive mismatches are still absorbed",
    w.state == runner.CLICKING and phone.sent == [],
)

# three is
phone = Recorder()
w = watcher(phone)
state["frame"] = SLOT
w.run(max_cycles=1)
text, png = phone.sent[0] if phone.sent else ("", None)
check(
    "three consecutive mismatches stop the clicking and send the screenshot",
    len(phone.sent) == 1
    and png is not None
    and "SLOT?" in text
    and w.state == runner.STOPPED
    and state["clicks"] == [BOOK_PX],  # BOOK was clicked, OK never was
    text.splitlines()[0] if text else "nothing sent",
)

# stopped, but not blind
state["clicks"].clear()
w.run(max_cycles=2)
check(
    "a stopped loop clicks nothing while the screen stays changed",
    state["clicks"] == [] and w.state == runner.STOPPED and len(phone.sent) == 1,
)

state["frame"] = BORING
w.run(max_cycles=config.DEBOUNCE)
check(
    "the boring screen coming back for three checks resumes the loop by itself",
    w.state == runner.CLICKING
    and len(phone.sent) == 2
    and "RESUMED" in phone.sent[1][0],
    phone.sent[1][0].splitlines()[0] if len(phone.sent) > 1 else "no second message",
)

check(
    "the resume message carries no screenshot, so it cannot be mistaken for an alert",
    phone.sent[1][1] is None,
)

# a form on screen keeps the loop off, with no presence detection anywhere
w.state, w.matches = runner.STOPPED, 2
state["frame"] = SLOT
w.run(max_cycles=1)
check(
    "the operator's own booking form keeps the loop from resuming underneath them",
    w.state == runner.STOPPED and w.matches == 0,
)
state["frame"] = BORING

# --- session drop -----------------------------------------------------------

phone = Recorder()
w = watcher(phone)
state["window"] = None
w.run(max_cycles=1)
check(
    "a vanished session window is as loud as a found slot, and stops the clicking",
    len(phone.sent) == 1
    and "DEAD SESSION?" in phone.sent[0][0]
    and w.state == runner.STOPPED
    and state["clicks"] == [],
    phone.sent[0][0].splitlines()[0] if phone.sent else "nothing sent",
)

phone = Recorder()
w = watcher(phone)
state["window"] = screen.Rect(200, 120, 1600, 900)  # someone moved it
w.run(max_cycles=1)
check(
    "a moved window is refused rather than clicked at the old coordinates",
    len(phone.sent) == 1 and w.state == runner.STOPPED and state["clicks"] == [],
    phone.sent[0][0].splitlines()[1] if phone.sent else "nothing sent",
)
state["window"] = WINDOW

# --- the operator lock ------------------------------------------------------

open(os.environ["NPC_OPERATOR_LOCK"], "w").close()
phone = Recorder()
w = watcher(phone)
w.run(max_cycles=3)
check(
    "an operator on the VM's display pauses the loop instead of fighting the cursor",
    state["clicks"] == [] and w.paused,
)
os.unlink(os.environ["NPC_OPERATOR_LOCK"])
w.run(max_cycles=1)
check("the loop picks up again when they disconnect", state["clicks"] == [BOOK_PX, OK_PX])

# --- guards -----------------------------------------------------------------


def expect_error(label, fn, needle):
    try:
        fn()
        check(label, False, "no error raised")
    except runner.Abort as abort:
        message = abort.payload.get("message", "")
        check(label, abort.payload["status"] == "error" and needle in message, message[:70])


state["frame"] = BORING[:900]
expect_error(
    "a changed display resolution refuses to run",
    lambda: runner.watch("demo", notifier=Recorder(), max_cycles=0, sleep=lambda s: None),
    "no longer mean anything",
)
state["frame"] = BORING

check(
    "the whole thing runs off what is on disk",
    runner.watch(
        "demo", notifier=Recorder(), emit=lambda e: None, max_cycles=1, sleep=lambda s: None
    )["clicks"]
    == 2,
)

with runner.RunLock(config.watch_lock()):
    try:
        runner.watch("demo", notifier=Recorder(), max_cycles=1, sleep=lambda s: None)
        check("a second watcher is refused", False)
    except runner.Abort as abort:
        check("a second watcher is refused", abort.payload == {"status": "busy"})

for bad, needle in [
    ("not json", "not valid JSON"),
    ("[]", "expected a JSON object"),
    ('{"ok": {"x_pct": 0.5, "y_pct": 0.5}}', "'book' must be an object"),
    ('{"book": {"x_pct": 1.4, "y_pct": 0.5}, "ok": {"x_pct": 0.5, "y_pct": 0.5}}', "0-1"),
    (
        '{"book": {"x_pct": 0.5, "y_pct": 0.5}, "ok": {"x_pct": 0.5, "y_pct": 0.5}, "debounce": 0}',
        "at least 1",
    ),
]:
    expect_error(f"rejects {bad[:34]!r}", lambda b=bad: runner.parse_plan(b, "test"), needle)

# --- Telegram ---------------------------------------------------------------

content_type, body = notify._multipart({"chat_id": "42", "caption": "hi"}, ("photo", "s.png", b"\x89PNG"))
boundary = content_type.split("boundary=")[1]
check(
    "the photo upload is a well-formed multipart body",
    body.startswith(f"--{boundary}\r\n".encode())
    and body.endswith(f"--{boundary}--\r\n".encode())
    and b'name="photo"; filename="s.png"' in body
    and b"\x89PNG" in body,
)

telegram = notify.Telegram("SECRET-TOKEN", "42")
check(
    "the bot token never reaches a log line",
    "SECRET-TOKEN" not in telegram._scrub("HTTP 401 at bot SECRET-TOKEN/sendPhoto"),
)

posted = []
telegram._post = lambda method, fields, file=None: (
    posted.append(method) or (method != "sendPhoto")
)
check(
    "a photo that will not upload still gets the message through as text",
    telegram.alert("wake up", png=b"x") and posted == ["sendPhoto", "sendMessage"],
    str(posted),
)

check(
    "no credentials means no notifier",
    isinstance(notify.from_env(("", "")), notify.Disabled)
    and isinstance(notify.from_env(("t", "42"), enabled=False), notify.Disabled)
    and isinstance(notify.from_env(("t", "42")), notify.Telegram),
)

# --- the CLI ----------------------------------------------------------------

out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = cli.watch(["--name", "demo"])
payload = json.loads(out.getvalue())
check(
    "watching with no way to send a message is refused, not done quietly",
    code == 1 and "NPC_TELEGRAM_TOKEN" in payload["message"],
    payload["message"][:70],
)

# --- the mouse path ---------------------------------------------------------


def trace(sx, sy, tx, ty):
    points = []
    stub.position = lambda: (sx, sy)
    stub.moveTo = lambda x, y: points.append((x, y))
    real_sleep, mouse.time.sleep = mouse.time.sleep, lambda s: None
    try:
        mouse.move(tx, ty)
    finally:
        mouse.time.sleep = real_sleep
    return points


for sx, sy, tx, ty in [(500, 500, 540, 500), (100, 100, 1500, 900)]:
    distance = math.hypot(tx - sx, ty - sy)
    deviations = []
    for _ in range(200):
        points = trace(sx, sy, tx, ty)
        assert points[-1] == (tx, ty), "the path must end exactly on the target"
        deviations.append(
            max(abs((tx - sx) * (sy - py) - (sx - px) * (ty - sy)) / distance for px, py in points)
        )
    worst = max(deviations)
    check(
        f"a {distance:.0f} px move arcs in proportion to its length",
        worst <= max(mouse.MAX_ARC, distance * 0.15) * 1.05,
        f"max {worst:.0f} px, {worst / distance * 100:.0f}% of travel, "
        f"median {statistics.median(deviations):.0f} px",
    )

check(
    "the same move never traces the same path twice",
    trace(100, 100, 1500, 900) != trace(100, 100, 1500, 900),
)

# --- the one-JSON-object contract holds even when the import dies -----------

# pyautogui's mouseinfo calls sys.exit() when tkinter is missing, which is a
# SystemExit rather than an Exception. A fresh interpreter is the only honest
# way to test the import path.
probe = """
import sys, importlib.abc
class Boom(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == 'pyautogui':
            sys.exit('simulated: tkinter is missing')
sys.meta_path.insert(0, Boom())
sys.argv = ['npc-watch', '--name', 'probe', '--no-telegram']
from npc import cli
sys.exit(cli.watch())
"""
proc = subprocess.run(
    [sys.executable, "-c", probe], capture_output=True, text=True, cwd=os.path.dirname(__file__)
)
try:
    payload = json.loads(proc.stdout)
except json.JSONDecodeError:
    payload = None
check(
    "a failed import still prints one JSON object, not an empty stdout",
    payload is not None and payload["status"] == "error" and proc.returncode == 1,
    (proc.stdout.strip() or f"stdout empty, stderr: {proc.stderr.strip()[:60]}")[:100],
)

# --- log --------------------------------------------------------------------

lines = config.log_path("demo").read_text().splitlines()
check(
    "every stop is logged with a timestamp, the name and the reason",
    any("status=changed" in line for line in lines)
    and any("status=session" in line for line in lines)
    and any("status=resumed" in line for line in lines),
    lines[-1][:80] if lines else "empty",
)

# --- the display bootstrap --------------------------------------------------

# Under systemd there is no XAUTHORITY and no ~/.Xauthority, and python-xlib
# reads that as "look in ~/.Xauthority" rather than "no cookie needed", then
# raises. An empty file is how you say the second thing. This is why the loop
# ran by hand and failed as a service, so it is asserted rather than assumed.
saved_env = {k: os.environ.get(k) for k in ("DISPLAY", "XAUTHORITY", "NPC_XAUTHORITY")}
for key in saved_env:
    os.environ.pop(key, None)
_, xauth = config.bootstrap_display(":77")
check(
    "with no cookie anywhere, XAUTHORITY points at an empty file rather than nothing",
    xauth == os.devnull and os.environ["XAUTHORITY"] == os.devnull,
    str(xauth),
)

with tempfile.NamedTemporaryFile() as real_cookie:
    _, xauth = config.bootstrap_display(":77", real_cookie.name)
    check("an explicit cookie is used as given", xauth == real_cookie.name, str(xauth))

os.environ["XAUTHORITY"] = "/nonexistent/cookie"
_, xauth = config.bootstrap_display(":77")
check(
    "an inherited XAUTHORITY that does not exist is not trusted",
    xauth == os.devnull,
    str(xauth),
)
for key, value in saved_env.items():
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


# --- the control endpoint ---------------------------------------------------

# A port that starts a mouse on someone else's screen is worth being paranoid
# about, so the routing and the auth are exercised without opening a socket.
from npc import control  # noqa: E402  (after the fakes, like everything else)

os.environ[control.TOKEN_ENV] = "s3cret-long-token"
systemctl_calls = []
control._systemctl = lambda *args: (systemctl_calls.append(args), (0, "active"))[1]
GOOD = "Bearer s3cret-long-token"

check("no Authorization header is refused", control.dispatch("GET", "/status", None)[0] == 401)
check("a wrong token is refused", control.dispatch("GET", "/status", "Bearer nope")[0] == 401)
check(
    "the right token without the Bearer prefix is refused",
    control.dispatch("GET", "/status", "s3cret-long-token")[0] == 401,
)
check("the right token is let in", control.dispatch("GET", "/status", GOOD)[0] == 200)
check(
    "starting takes a POST, so a crawled link cannot do it",
    control.dispatch("GET", "/start", GOOD)[0] == 404,
)
check("POST /start starts", control.dispatch("POST", "/start", GOOD)[0] == 200)
check("POST /stop stops", control.dispatch("POST", "/stop", GOOD)[0] == 200)
check("an unknown path is a 404", control.dispatch("POST", "/rm-rf", GOOD)[0] == 404)
check(
    "the endpoint only ever asks systemd about the one unit",
    {call[1] for call in systemctl_calls} == {"npc"}
    and {call[0] for call in systemctl_calls} <= {"start", "stop", "is-active"},
    str(systemctl_calls),
)

os.environ[control.TOKEN_ENV] = ""
check("an empty token lets nobody in", control.dispatch("GET", "/status", "Bearer ")[0] == 401)
try:
    control.serve()
    check("without a token it refuses to listen at all", False)
except control.ControlError as exc:
    check("without a token it refuses to listen at all", "worse than no start button" in str(exc))

print()
if failures:
    print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
