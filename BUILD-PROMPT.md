# Build prompt — the laptop agent

Build **one thing**: a program that runs on a dedicated Ubuntu laptop, drives its
browser by clicking coordinates like a hand would, and is reachable only over SSH.

Nothing else is in scope. The GCP server is not built here — it is only a caller,
and its interface is specified below. The human's viewer is not built here either —
it is any VNC client over an SSH tunnel.

---

## Operating context

This automates the operator's own routine on their own real accounts, through **one
Google account** signed in by hand and left to accumulate genuine history.

The goal is therefore **consistency, not evasion.** Nothing rotates, nothing is
spoofed. The browser is an ordinary Chrome that a person happens to use through a
remote screen. There is no automation surface in it at all — no debug port, no
driver, no injected script. Input arrives as real X11 events, which is why this
works.

The laptop reaches the internet through a **physical SIM**. Carrier NAT means no
inbound connection is possible, which is fine: SSH out, or SSH in through the
operator's own network path. Mobile data is metered, so the screen stream is the
only expensive thing here — the automation itself sends almost nothing.

---

## Hard constraints

1. **SSH is the only way in.** No HTTP server, no open ports, no web framework.
   Everything binds `127.0.0.1` or nothing at all.

2. **No browser automation library.** No Playwright, no Selenium, no CDP, no
   `--remote-debugging-port`. Chrome is started as an ordinary browser and is never
   told it is being automated. The only thing that touches it is the mouse.

3. **The X session is Xorg, not Wayland.** Wayland blocks both screen capture and
   input injection, which are the two things this program does.

4. **Chrome uses a persistent profile directory.** It holds the accumulated cookies
   and session trust and cannot be rebuilt. The README must say how to back it up
   off the machine.

5. **Small.** No more than about five source files. If it needs a framework, the
   design is wrong.

6. **Python.** `pyautogui` for input, `mss` for screen capture,
   `opencv-python-headless` and `numpy` for comparison. Nothing else.

   Use `mss` rather than `pyautogui.screenshot()` for capture: it talks to X11
   directly at roughly 5–15 ms, where pyautogui's path may shell out to `scrot` or
   `gnome-screenshot` and fail confusingly if neither is installed. Capture the
   primary monitor only, not the union of all screens.

---

## The two ways in

### Option 1 — the operator drives

The operator opens an SSH tunnel from another computer and connects a VNC client to
the forwarded port:

```bash
ssh -N -L 5900:127.0.0.1:5900 user@laptop
```

They now have the whole desktop — browser, settings, terminal — and can do anything,
including solving a CAPTCHA or logging in.

**While a VNC client is connected, automation is off.** Any run requested by GCP
exits immediately with `{"status": "locked"}` and does nothing. Two things driving
one cursor is the failure mode this prevents, and connection is the signal — not
input detection, which would be guesswork.

Implement the lock with x11vnc's own hooks: `-afteraccept` writes a lock file,
`-gone` removes it. The run command checks for that file before doing anything.

### Option 2 — GCP drives

GCP runs the agent over SSH, piping a scenario in and reading the result out:

```bash
ssh user@laptop npc-run --name checkout --scenario - < scenario.json
```

`--name` identifies the scenario and is what the reference screenshots are filed
under. Same name, same references.

There is no daemon and no API. SSH is the transport, the authentication and the
session. The agent is a command that runs, prints one JSON object, and exits.

**GCP has no interface and never sees the screen.** No VNC, no stream, no display
connection of any kind — the live picture belongs to the operator alone. GCP sends
JSON in and reads JSON out. The only image it ever receives is a single still PNG
returned when a step mismatches, and in the normal case it receives none at all.

The two options are not symmetrical: one is a screen for a human, the other is a
command with a JSON result. They share only SSH.

**One invocation per scenario run, not per step.** A scenario is a whole sequence,
so a single SSH handshake wraps thirty seconds to several minutes of work. Do not
build a daemon, a persistent session or a streaming protocol to avoid that
handshake — on a mobile link a long-lived connection is reaped by carrier NAT and
breaks more often than a short one. If connection setup ever needs removing, SSH's
own `ControlMaster`/`ControlPersist` multiplexing does it with no code.

**Known limitation, accepted for now:** the run is tied to the SSH channel, so if
the connection drops mid-scenario the agent dies partway through a click sequence
and the browser is left in an unknown state. Log it and let the operator look. Do
not build detached runs, resumption or state recovery yet.

---

## Runtime environment — read this before writing code

A non-interactive SSH command does **not** inherit the graphical session, and this
is the single most likely thing to break the build.

**The X display.** `ssh user@laptop npc-run …` starts with no `DISPLAY` and no
access to the desktop's X credentials, so `pyautogui` fails with "cannot connect to
display". The agent must set both itself, before importing anything that touches X:

```python
os.environ["DISPLAY"] = ":0"
os.environ["XAUTHORITY"] = "/home/<user>/.Xauthority"
```

Make both configurable — some setups place the Xauthority file under `/run/user/…`
instead. If the agent cannot reach the display, it must exit with a clear
`{"status": "error"}` saying so, not a Python traceback.

**PATH.** A non-interactive SSH command does not source `.bashrc`, so a virtualenv
on the interactive PATH will not be found. Either install `npc-run` to a system path
or have GCP call the venv's interpreter by absolute path. State which in the README.

**Chrome is not the agent's business.** It runs as part of the desktop session,
started by the operator or by autostart, with its persistent profile. The agent
never launches it, never restarts it, and never talks to it — it only moves the
mouse. Keeping this boundary is what keeps the browser clean.

**x11vnc runs as part of the desktop session too**, bound to loopback, with the
hooks that drive the lock:

```bash
x11vnc -display :0 -rfbport 5900 -localhost -forever -shared -noxdamage \
       -afteraccept 'touch /tmp/npc-operator-present' \
       -gone        'rm -f /tmp/npc-operator-present'
```

`-localhost` is what makes SSH the only way to reach it. Nothing else is needed —
no password, because nothing is exposed.

---

## The scenario format

A flat list of steps. Nothing nested, no conditionals, no selectors.

```json
[
  {"action": "click", "x": 512, "y": 340},
  {"action": "wait",  "seconds": 3},
  {"action": "click", "x": 880, "y": 612},
  {"action": "wait",  "seconds": 2}
]
```

`click` and `wait` are the only actions. **No keyboard, no typing** — not needed yet.

---

## The run loop

```
for each step:
    if a VNC client is connected  → exit {"status": "locked"}

    if action is wait  → sleep, continue

    screenshot
    if this step has no stored reference:          # first run
        store the screenshot as the reference
        click
    else:
        compare screenshot against the reference
        ├─ below threshold → click the stored coordinate
        └─ above threshold → exit {"status": "mismatch", ...} with the screenshot
```

**The first run records.** It executes the coordinates it was given and saves the
screen as it looked before each click — that is the state in which this coordinate
was known to be correct.

**Every later run verifies.** It only clicks when the screen still looks like it did
when the coordinate worked.

**Verify the end state too.** Each step's check validates the *previous* step's
outcome — so nothing validates the last one. After the final action, wait, take one
more screenshot and compare it against a stored final reference. Without this a
scenario whose last click silently failed still reports `ok`, which is the worst
possible lie for this system to tell.

**Refuse to run twice at once.** Two concurrent runs would fight over one cursor,
exactly like the operator-versus-GCP case. Take a lock file for the duration of a
run and exit `{"status": "busy"}` if it is already held. Release it on exit,
including on error.

In the normal case the agent sends nothing back but a status line. A screenshot
crosses the network only when something has actually changed.

---

## Screenshot comparison

Do not compare whole screenshots byte for byte — they never match. The cursor
moves, clocks tick, spinners spin, ads reload. A popup differs by far more than that
noise, and separating the two is the entire job.

**Method** (benchmarked at roughly 2 ms; the screenshot capture itself costs
10–30 ms, so this is not worth optimising further):

1. Downsample both images to 480×270 greyscale.
2. Split into a 16×9 grid of 30×30-pixel tiles.
3. Per tile, compute mean absolute difference. Mark the tile changed if it exceeds
   ~12/255.
4. The result is the **fraction of tiles changed**. Escalate when it exceeds the
   threshold.

Tiles matter more than a single number because they say *where* the change is:
a couple of scattered tiles is cursor and clock noise; a contiguous cluster is a
popup or modal; most tiles changed means the wrong page entirely.

**Calibrating the threshold.** Do not hard-code a guess. Capture five to ten
screenshots of the same settled state and measure the changed-tile fraction between
them — that is the noise floor. Set the threshold at roughly three times it. Make it
configurable per scenario.

Rejected alternatives, for the record: perceptual hashing is too coarse (a clear
popup measured 6/64 Hamming, inside the ambiguous band), and local embedding models
cost 20–100× more while being *less* discriminative, because they are built to
judge semantic similarity and will call two screenshots of the same page "the same"
whether or not a modal is covering it.

If ads or a clock cause repeated false escalations, the first refinement is a noise
mask: during calibration, permanently exclude tiles that vary between identical
states. Do not build this until you see the problem.

---

## Mouse movement — the click mechanism

This is the whole actuation layer. It takes a coordinate and clicks it like a hand
would. **No keyboard. No typing. Nothing else.**

A straight-line jump followed by an instant click is the clearest behavioural tell
there is. The fix is a curve and some jitter, and it is about twenty lines:

```python
import random, time, pyautogui

pyautogui.FAILSAFE = True   # slam the cursor into the top-left corner to abort

def move(x, y, steps=13):
    sx, sy = pyautogui.position()
    # Quadratic Bezier: the control point sits off the midpoint, so the path
    # arcs instead of running straight.
    cx = (sx + x) / 2 + random.randint(-80, 80)
    cy = (sy + y) / 2 + random.randint(-80, 80)
    for i in range(steps + 1):
        t = i / steps
        bx = (1-t)**2 * sx + 2*(1-t)*t * cx + t**2 * x
        by = (1-t)**2 * sy + 2*(1-t)*t * cy + t**2 * y
        pyautogui.moveTo(int(bx), int(by))
        time.sleep(random.uniform(0.005, 0.015))

def click(x, y):
    move(x, y)
    time.sleep(random.uniform(0.01, 0.05))   # a hand settles before pressing
    pyautogui.click()
```

Why each part earns its place:

- **The Bezier curve** gives a curved approach. Real pointers never travel straight.
- **The random control point** means the same click never traces the same path.
- **Jittered per-step sleep** (5–15 ms) breaks machine-regular timing. The whole
  move takes roughly 140 ms, which is human-plausible.
- **The settle pause** before pressing mirrors the gap between arriving and clicking.
- **`FAILSAFE`** aborts everything if the cursor reaches the top-left corner. Keep
  it; it is the manual override when a run goes wrong.

`pyautogui` uses the X11 XTEST extension underneath, so these are real OS-level
input events — indistinguishable from a physical mouse and `isTrusted` in the
browser.

**One fix on the original:** the ±80 px control-point offset is a fixed constant, so
a 40 px move arcs absurdly wide. Scale it with distance — roughly 15% of travel,
capped at 80 — and scale `steps` the same way. Two lines, and short moves stop
looking bizarre.

---

## Output contract

The agent prints exactly one JSON object to stdout and exits.

```json
{"status": "ok",       "steps": 4}
{"status": "locked"}
{"status": "mismatch", "step": 2, "diff": 0.34, "screenshot": "<base64 png>"}
{"status": "error",    "step": 1, "message": "..."}
```

Log every mismatch and error to a local file with a timestamp, the scenario name and
the step index. A Telegram notification that calls the operator in to fix the state
comes later; the log is what it will read.

---

## Storage layout

```
~/.npc/
  scenarios/<name>.json         the steps
  refs/<name>/step-<n>.png      the reference screenshot for each step
  logs/<name>.log               mismatches and errors
```

Reference screenshots are written on the first run and then left alone. **Do not
overwrite them automatically** — a self-healing reference can drift silently onto
the wrong element, and you will not notice until it clicks something it shouldn't.

References are only valid at the screen resolution they were captured at. Record the
resolution alongside them and refuse to run — with a clear error — if it has since
changed, rather than clicking coordinates that no longer mean anything.

---

## Machine setup the README must cover

Each of these presents as "the system is broken" when the operator connects:

- Disable screen lock, blanking and automatic suspend, or you connect to a lock
  screen instead of a desktop.
- Enable automatic login, so a power cut returns to a working session unattended.
- Set `HandleLidSwitch=ignore` in `/etc/systemd/logind.conf`, or closing the lid
  suspends the machine.
- Set the system timezone to match where the SIM actually is.
- Fix the browser window geometry and never move it. Every coordinate depends on it.
- How to find coordinates when writing a scenario by hand. There is no recorder in
  scope, so document the manual route: hover the target and read the position with
  `xdotool getmouselocation`, or open a screenshot and read the pixel off it.
  Without this, authoring the very first scenario is guesswork.

---

## Acceptance test

1. From another computer, `ssh -L 5900` and connect a VNC client. You can drive the
   desktop.
2. **While still connected**, have GCP run a scenario over SSH. It returns
   `{"status": "locked"}` and nothing moves.
3. Disconnect the viewer. Run a three-step scenario over SSH. It executes, the
   cursor visibly arcs to each target, and three reference screenshots appear under
   `~/.npc/refs/`.
4. Run the same scenario again. It completes with `{"status": "ok"}` and returns no
   image.
5. Open a popup on the page by hand, then run it again. It returns
   `{"status": "mismatch"}` with the screenshot, and clicks nothing.

If all five pass, stop building.

---

## Explicit non-goals

- The GCP server, the VLM, and any decision about *where* to click
- Keyboard input of any kind
- Scenario recording tools, element detection, OCR
- Self-healing references
- Telegram notifications
- Any daemon, HTTP API, queue, database or retry logic
- Anti-detection beyond the mouse movement above — there is one real account
  behaving consistently, and nothing to hide

---

## Deliverable

A `README.md` with the exact commands to install, run and back up, and no more than
about five source files. State plainly what is not implemented.
