# Update prompt — relocate the agent to a GCP virtual display driving RustDesk

You have already built the system described in `BUILD-PROMPT.md`: a Python agent that
screenshots a screen, compares it against stored references, and clicks coordinates
with a Bezier-curved mouse path.

**The core is right. Where it runs and what it clicks are wrong.** This document is
the delta. Read `BUILD-PROMPT.md` first, then apply these changes.

---

## The actual job, which was never stated before

A hospital appointment queue in Australia releases slots unpredictably. They are
taken within minutes. Someone has to sit there clicking **BOOK**, receiving "all
appointments are currently booked", clicking **OK**, and repeating — for hours,
through the Australian night.

The operator is in the United States, at work, and cannot do this. Their son, whose
appointment it is, owns the machine in Australia and cannot be asked to install or
configure anything.

So: a server clicks tirelessly. When the response finally differs — a slot exists —
it messages the operator, who takes over by hand to fill in personal details.

Everything below follows from that.

---

## The new topology

```
Son's laptop (Australia)          RustDesk HOST. Browser, queue page.
   ▲                    ▲          Nothing custom installed. Never touched again.
   │ automation         │ operator, on demand
   │                    │
GCP VM (Sydney)         Operator's laptop (US)
Xvfb + RustDesk CLIENT  RustDesk only. Runs nothing.
+ the loop              Waits for a Telegram message.
```

The agent no longer touches a browser. It drives a **RustDesk client window** on a
virtual display, which mirrors a screen 8,000 km away. Everything it sees and clicks
happens inside that window.

The operator's machine runs nothing at all. That is a hard requirement: they are at
work, and an automation that moves their physical cursor is unusable.

Put the VM in **australia-southeast1 (Sydney)**. Proximity to the son keeps the
RustDesk stream sharp and the loop responsive. Proximity to the operator buys
nothing — they only read Telegram.

---

## Constraints that REVERSE

These contradict `BUILD-PROMPT.md` directly. The new instruction wins.

| Was | Now |
|---|---|
| "Do not use Xvfb — share the real desktop `:0`" | **Xvfb is required.** The VM has no physical screen, and the automation must never move a real cursor. |
| "Chrome runs on the machine, the agent never touches it" | **No Chrome on this machine at all.** The browser is on the son's laptop. The agent's target is the RustDesk client window. |
| "SSH is the only way in; GCP invokes `npc-run` per scenario" | **The loop runs continuously on the VM itself**, started once as a service. Nothing invokes it remotely. There is no `--scenario` stdin contract any more. |
| "A mismatch is an error — log it and stop" | **A mismatch is the success condition.** It is the event the entire system exists to detect. |
| "Anti-detection matters: residential IP, real GPU, real fingerprint" | **Irrelevant now.** The browser is the son's real browser, on his real machine, on his real connection. There is nothing to engineer. Delete that reasoning; do not carry it forward. |

---

## What to KEEP unchanged

- The tiled screenshot comparison: 480×270 greyscale, 16×9 grid, ~12/255 per tile,
  fraction-of-tiles-changed, threshold calibrated from repeated captures of the
  settled state. This was benchmarked and is correct.
- The Bezier mouse movement with jittered timing, including the distance-scaling fix.
- `mss` for capture, `pyautogui` for input, `opencv-python-headless` + `numpy`.
- `pyautogui.FAILSAFE = True`.
- Reference screenshots written once and never auto-overwritten.
- The resolution guard: refuse to run if the screen size differs from when the
  references were recorded.
- The storage layout under `~/.npc/`.
- Small: about five source files, no framework.

---

## The new run loop

The scenario is no longer a linear list executed once. It is **two clicks repeated
indefinitely until the screen stops looking boring.**

```
setup (once, by hand):  RustDesk client connected, fullscreen, window never moved
                        references recorded for the "all booked" response

loop forever:
    click BOOK        (recorded coordinate, Bezier path)
    wait              (recorded seconds — the page needs to respond)
    screenshot
    compare against the "all booked" reference
    ├─ matches   → click OK, wait, continue looping
    └─ differs   → send Telegram, STOP FOREVER
```

**The reference is the boring state.** Matching it means "nothing has happened, keep
going". Deviating from it means "something happened, wake the human". This is the
inversion of the old design and the single most important change in this document.

**A single mismatch is not an event. Require N consecutive mismatches.** Three is a
reasonable default. The image arrives over a video codec whose compression varies
with bandwidth, so an unchanged screen still produces some pixel churn, and a
reconnect or a momentary artefact will differ wildly for one frame. None of those
survive three checks a few seconds apart; a real slot does. This debounce is the
primary defence against false alarms — more effective than trying to freeze
RustDesk's encoder settings.

**After alerting, stop clicking but keep watching.** Do not exit. Re-compare once a
minute:

- If the screen returns to the boring reference and stays there for three checks,
  **resume clicking and send a second Telegram saying so.** This is what recovers
  automatically from a RustDesk reconnect, a transient dialog, or any false alarm —
  without the operator having to do anything at 3am.
- If it does not, stay stopped and keep waiting.

This is safe by construction: while the operator is filling in the booking form, the
screen shows that form, which does not match the boring reference — so the loop
cannot resume underneath them. No presence detection is needed; the comparison
already encodes it.

---

## What to ADD

### Telegram notification

Previously a non-goal. Now it is the entire point — the loop's output is a message
to a human.

On the third consecutive mismatch, send: the scenario name, the timestamp, the
measured difference, and **the screenshot itself** so the operator can tell a real
slot from a false alarm before they scramble.

Send a second, clearly different message when the loop resumes on its own, so an
operator who was woken at 3am knows the situation resolved without them.

Bot token and chat ID from environment variables. Nothing more elaborate.

### Session-drop detection

The failure that will actually hurt is silent: the son's laptop sleeps, reboots, or
loses wifi, RustDesk disconnects, and the loop keeps clicking a dead window for
hours while the operator believes it is working.

Detect it and Telegram it with a different message. **A dead session must be as loud
as a found slot.** If distinguishing a disconnect from a page change is awkward,
alert on both and let the screenshot tell the operator which it is — with minutes of
slack, a false alarm costs nothing and a silent failure costs everything.

### Setup access to the VM's own display

The operator needs to reach the VM's virtual desktop by hand to connect RustDesk the
first time, enter the son's ID and password, position the window, and record
coordinates.

Keep `x11vnc` bound to loopback, reached over an SSH tunnel, exactly as before. Its
operator lock stays too — but note what it now guards: the **VM's own display**, so
a debugging connection does not fight the automation for the cursor. It has nothing
to do with the son's machine.

---

## RustDesk on Xvfb — tested, it works

This was the one architectural unknown. It has been verified empirically: the
RustDesk **1.4.9 Flutter client** launches and renders its full UI on
`Xvfb :99 -screen 0 1920x1080x24` with no GPU, inside a Debian bookworm container.
The window appears, the ID and password panel draws correctly, and the status line
reaches *Ready* against the public rendezvous server. Mesa's software rasterizer is
sufficient — no hardware acceleration is required.

**Do not use the `-sciter` build.** It exists as a lighter-weight fallback, but the
standard Flutter build works, so use it.

What the environment must provide:

```
xvfb  openbox  libgl1-mesa-dri  libglx-mesa0  libegl-mesa0
```

`openbox` matters: RustDesk needs focus handling, and its dialogs misbehave without
a window manager.

**Two things the test surfaced that the build must handle:**

1. **Set `HOME` and the XDG directories explicitly.** The client logs
   `MissingPlatformDirectoryException: Unable to get application documents
   directory`. It runs anyway, but this is where it persists configuration and saved
   peers — leave it broken and the client may not remember the host between
   restarts, which breaks unattended operation.

2. **The window opens centred at roughly 800×590, not fullscreen.** Every coordinate
   depends on its position and size, so this must be pinned — but **do not make it
   fullscreen.** Flutter on Linux reports incorrect display information via
   `MediaQuery` under Xvfb specifically when fullscreen
   (flutter/flutter#162801); the documented workaround is to run windowed.
   Resize it to a fixed large size and strip decorations with an openbox rule
   instead, then verify the geometry before recording any coordinates.

3. **The remote session opens in a SEPARATE top-level window.** The Flutter client
   uses a multi-window architecture — connecting does not fill the main window, it
   spawns another one. The capture-and-click code must resolve the correct window
   (`xdotool search --name`) rather than assuming a single window exists, and must
   handle it appearing after connection rather than at launch.

4. **Kill any running instance before launching with `--connect`.** RustDesk reports
   "Key mismatch" errors when `--connect <id> --password <pw>` is used while a GUI
   instance is already running (rustdesk#13693, rustdesk#10088). Make process
   cleanup part of startup, not something to remember.

### Environment to set

```
LIBGL_ALWAYS_SOFTWARE=1      # what RustDesk's own allow-always-software-render does
GALLIUM_DRIVER=llvmpipe      # insurance if driver selection misbehaves
GDK_BACKEND=x11
XDG_RUNTIME_DIR=<a real writable directory>
```

Run the client under `dbus-run-session` — it uses D-Bus for URI forwarding and
complains without a session bus.

Start Xvfb with the extensions explicit rather than trusting defaults:

```bash
Xvfb :99 -screen 0 1920x1080x24 +extension GLX +extension RANDR +extension RENDER -noreset
```

**Check Mesa before launching RustDesk at all.** If this does not report llvmpipe,
nothing else will work and no environment variable will rescue it:

```bash
DISPLAY=:99 glxinfo -B | grep -E "renderer|OpenGL version"   # want: llvmpipe, OpenGL 4.5
```

**Still unproven, and worth checking early:** the test confirmed the client renders
its own UI, not that it renders a *live remote session* at usable framerate under
software rendering. Video decode is CPU-side VP8/VP9 and should be fine, but confirm
it with a real host before building the loop on top.

**Fallbacks, in order, if the Flutter build disappoints against a live session:**
the `-sciter` build (Cairo/CPU, no GL dependency at all, and a single-window model
that avoids gotcha 3 — but deprecated upstream); a real Xorg with
`xserver-xorg-video-dummy` instead of Xvfb, which has fuller GLX and DRI plumbing;
or TigerVNC's `Xvnc`, which is also a real X server and lets you watch what is
happening instead of debugging blind through screenshots.

---

## RustDesk settings that must be pinned during setup

These live inside the RustDesk session, not in your code, and getting them wrong
produces silently wrong behaviour rather than errors.

**Prefer a fixed image quality**, since RustDesk otherwise varies compression with
bandwidth and an unchanged screen still churns pixels. But do not rely on it — the
comparison tolerance and the three-strike debounce are the real defence, and they
have to work regardless because compression artefacts are inherent to receiving the
screen over a video codec. **The image RustDesk renders is the only source of truth
available.** There is no agent on the son's machine and no way to capture his screen
directly; that is the deliberate cost of him installing nothing.

**Scale is handled by normalising, not by pinning.** Two mechanisms, both already in
the design:

- *For comparison:* both images are downsampled to a fixed 480×270 before diffing,
  so a change in the rendered scale is normalised away automatically as long as the
  aspect ratio holds.
- *For coordinates:* store them as **percentages of the content rectangle**, not
  absolute pixels, and multiply by the current size at click time. This is already
  the pattern in v1 — see `w_pct` / `h_pct` in `eye.py` and `memory.py`.

The one trap: percentages must be relative to the **content**, not the window. If
RustDesk letterboxes the remote desktop with black bars, measuring from the window
edge puts every coordinate out by the bar width. Detect the content rectangle at
setup and record it alongside the references.

**The unattended access password is a credential.** It reaches the VM through an
environment variable or a file with restricted permissions — never hardcoded in a
source file, never written to the log, and never included in the Telegram message
or the screenshot caption.

---

## Recording coordinates in this architecture

More confusing than before, because the target is a remote screen rendered inside a
local window. The workflow:

1. Connect to the VM's virtual display with the VNC tunnel.
2. Position and size the RustDesk window, then leave it alone forever.
3. Hover each target inside the remote view and read the position with
   `xdotool getmouselocation` — this gives the coordinate **on the VM's virtual
   display**, which is what the agent will click. Do not use coordinates from the
   son's own screen; they are a different space.
4. Capture the "all booked" reference in the settled state.

Restarting after a stop is manual and should stay manual: the operator connects,
confirms the remote screen is in the expected state, and starts the loop again.
Do not add a scheduler or an auto-restart.

---

## Known limitations to accept, not solve

- **A RustDesk reconnect looks like a screen change.** Do not build logic to
  distinguish it from a real one — the three-strike debounce absorbs the brief
  version, and the auto-resume recovers from the longer version by itself once the
  boring state returns. You may still get one Telegram out of it. That is the
  correct amount of effort to spend here.
- **The Bezier path is resampled by RustDesk's protocol** on the way to Australia,
  so what arrives is coarser than what was drawn. Keep the Bezier — it costs
  nothing and the jittered timing survives — but do not add complexity trying to
  preserve path fidelity across the wire.
- **Coordinates depend on the RustDesk window staying fullscreen, unmoved, and on
  the son's screen resolution not changing.** Guard it with the resolution check;
  do not attempt to re-locate the window automatically.

---

## Updated acceptance test

1. On the VM, Xvfb is running with the RustDesk client **windowed at a fixed size —
   not fullscreen** — decorations stripped, and connected to the son's machine. A
   screenshot of the virtual display shows his desktop.
2. Record the "all booked" reference and the two coordinates **as percentages of the
   content rectangle**.
3. Start the loop. It clicks BOOK, waits, sees the familiar response, clicks OK, and
   repeats — visibly, for at least twenty cycles.
4. Change the remote screen by hand (open any window on the son's machine). After
   **three consecutive** mismatches the loop stops clicking and a Telegram arrives
   with the screenshot attached.
5. Restore the remote screen to the "all booked" state. After three consecutive
   matches the loop **resumes on its own** and a second Telegram says so.
6. Kill the RustDesk connection. A Telegram arrives reporting a dead session.

If all six pass, stop building.

---

## Non-goals, unchanged

No VLM, no OCR, no element detection, no keyboard input, no scenario recorder, no
self-healing references, no database, no retry logic, no resume. Two buttons at two
fixed coordinates is the whole job.
