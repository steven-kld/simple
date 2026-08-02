# npc — the appointment watcher

A hospital queue in Australia releases appointment slots unpredictably, and they
are taken within minutes. Someone has to sit there clicking **BOOK**, reading
"all appointments are currently booked", clicking **OK**, and repeating, through
the Australian night.

This is the server that does that instead. It runs on a GCP VM in Sydney, drives
a **RustDesk client window on a virtual display**, and clicks two buttons on a
laptop 8,000 km away. When the response finally differs — a slot exists — it
sends a Telegram message with the screenshot and stops clicking, so the operator
can take over and fill in the details by hand.

```
Son's laptop (Australia)          RustDesk HOST. Browser, queue page.
   ▲                    ▲          Nothing custom installed. Never touched again.
   │ automation         │ operator, on demand
   │                    │
GCP VM (Sydney)         Operator's laptop (US)
Xvfb + RustDesk CLIENT  RustDesk only. Runs nothing.
+ this program          Waits for a Telegram message.
```

The VM goes in **australia-southeast1**. Proximity to the son keeps the RustDesk
stream sharp and the loop responsive; proximity to the operator buys nothing,
because they only read Telegram.

**The reference is the boring state.** Matching it means nothing has happened,
keep going. Deviating from it means something happened, wake the human. That
inversion is the whole design.

---

## The files

| File | What is in it |
| --- | --- |
| [npc/config.py](npc/config.py) | Paths, defaults and the DISPLAY bootstrap |
| [npc/screen.py](npc/screen.py) | Capture, the RustDesk window, the content rectangle, the tiled comparison |
| [npc/mouse.py](npc/mouse.py) | The Bezier move and the click |
| [npc/notify.py](npc/notify.py) | Telegram, over urllib, with a photo-to-text fallback |
| [npc/runner.py](npc/runner.py) | Setup, the watch loop, the lock, the log |
| [npc/cli.py](npc/cli.py) | `npc-setup`, `npc-watch`, `npc-calibrate` |
| [selftest.py](selftest.py) | The whole loop against fake frames, with no display and no input |
| [docs/index.html](docs/index.html) | A stand-in queue page, for rehearsing the loop locally |
| [docs/state.json](docs/state.json) | The rehearsal's remote switch, polled by that page |

---

## The loop

```
setup (once, by hand):  RustDesk client connected, windowed at a fixed size,
                        window never moved again
                        references recorded for the "all booked" response

loop forever:
    click BOOK        (recorded coordinate, Bezier path)
    wait              (recorded seconds — the page needs to respond)
    screenshot
    compare against the "all booked" reference
    ├─ matches   → click OK, wait, keep going
    └─ differs   → three times in a row? Telegram, and stop clicking
```

**A single mismatch is not an event.** The picture arrives over a video codec
whose compression varies with bandwidth, so an unchanged screen still churns
pixels, and a reconnect or a momentary artefact differs wildly for one frame.
None of that survives three checks four seconds apart; a real slot does. This
debounce is the primary defence against false alarms — more effective than
trying to freeze RustDesk's encoder settings.

**After alerting it stops clicking but keeps watching.** It does not exit. Once
a minute it re-compares:

- if the boring screen comes back and stays for three checks, it **resumes
  clicking and sends a second Telegram saying so** — which is how a RustDesk
  reconnect, a transient dialog or a false alarm recovers with nobody awake;
- otherwise it stays stopped.

This is safe by construction. While the operator is filling in the booking form
the screen shows that form, which does not match the boring reference, so the
loop cannot resume underneath them. No presence detection is needed; the
comparison already encodes it.

**A dead session is as loud as a found slot.** The failure that actually hurts
is silent: the son's laptop sleeps, RustDesk disconnects, and the loop clicks a
dead window for hours while the operator believes it is working. So the session
window's presence and geometry are checked every cycle, and losing either sends
its own message.

---

## From zero to a running loop

In order. Each step is a section below.

1. [Create the VM](#install-on-the-vm) in australia-southeast1 and install the packages.
2. [Start Xvfb, openbox and x11vnc](#the-virtual-display), and check Mesa reports llvmpipe.
3. [Start RustDesk](#the-virtual-display) and connect it to the son's machine **by hand**,
   over the [VNC tunnel](#reaching-the-vms-display). Size the window, then never touch it again.
4. Get the queue page to its "all booked" state, then
   [record the reference and the two coordinates](#setup-recording-the-coordinates-and-the-reference).
5. [Calibrate the threshold](#calibrating-the-threshold).
6. [Run it](#running-it) with the Telegram variables set, then install the systemd unit.
7. Walk the [acceptance test](#acceptance-test).

If you only want to see the machinery work, skip all of that and do the
[local rehearsal](#a-local-rehearsal-no-vm-no-australia) first — it needs no GCP,
no RustDesk and no Australia.

---

## Install on the VM

Debian bookworm or Ubuntu. No GPU, no physical screen. `e2-standard-2` is
enough: the work is software rasterising a video stream, which is CPU.

```bash
gcloud compute instances create npc-watcher \
  --zone=australia-southeast1-b \
  --machine-type=e2-standard-2 \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=20GB

gcloud compute ssh npc-watcher --zone=australia-southeast1-b
```

No firewall rule is needed and none should be added: nothing listens on a public
interface. x11vnc binds loopback and is reached through the SSH tunnel that
`gcloud compute ssh` already gives you.

```bash
sudo apt install -y xvfb openbox x11vnc xdotool mesa-utils dbus-x11 \
                    libgl1-mesa-dri libglx-mesa0 libegl-mesa0 \
                    python3-venv python3-tk

python3 -m venv ~/.npc-venv
~/.npc-venv/bin/pip install /path/to/simple
sudo ln -sf ~/.npc-venv/bin/npc-watch     /usr/local/bin/npc-watch
sudo ln -sf ~/.npc-venv/bin/npc-setup     /usr/local/bin/npc-setup
sudo ln -sf ~/.npc-venv/bin/npc-calibrate /usr/local/bin/npc-calibrate
```

`openbox` matters: RustDesk needs focus handling and its dialogs misbehave
without a window manager. `python3-tk` is a hard requirement of pyautogui — one
of its dependencies calls `sys.exit()` at import time when tkinter is absent.
Everything Python is in `pyproject.toml`, so the venv is disposable.

Check the install without a display:

```bash
~/.npc-venv/bin/python selftest.py     # 44 checks, no X server needed
```

If `python3-tk` is missing you will not get a traceback, you will get this —
which is the same shape as every other failure here, on purpose:

```json
{"status":"error","message":"the agent could not start: NOTE: You must install tkinter on Linux to use MouseInfo. ..."}
```

### RustDesk

Install the **1.4.9 Flutter client**. Do *not* use the `-sciter` build: it exists
as a lighter fallback, but the standard build works.

```bash
wget https://github.com/rustdesk/rustdesk/releases/download/1.4.9/rustdesk-1.4.9-x86_64.deb
sudo apt install -y ./rustdesk-1.4.9-x86_64.deb
```

---

## The virtual display

```bash
Xvfb :99 -screen 0 1920x1080x24 +extension GLX +extension RANDR +extension RENDER -noreset &
DISPLAY=:99 openbox &
```

The extensions are explicit rather than trusted to defaults. **Check Mesa before
launching RustDesk at all** — if this does not say llvmpipe, nothing else will
work and no environment variable will rescue it:

```bash
DISPLAY=:99 glxinfo -B | grep -E "renderer|OpenGL version"   # want llvmpipe, OpenGL 4.5
```

Then the client. Every line here earns its place:

```bash
#!/bin/bash
# ~/bin/rustdesk-client.sh
export DISPLAY=:99
export HOME=/home/npc                       # or the client cannot persist its config
export XDG_CONFIG_HOME=$HOME/.config
export XDG_DATA_HOME=$HOME/.local/share
export XDG_RUNTIME_DIR=/run/user/$(id -u)   # must exist and be writable
export LIBGL_ALWAYS_SOFTWARE=1              # RustDesk's own allow-always-software-render
export GALLIUM_DRIVER=llvmpipe              # insurance if driver selection misbehaves
export GDK_BACKEND=x11

pkill -x rustdesk; sleep 2                  # see "Key mismatch" below
dbus-run-session -- rustdesk &              # D-Bus is used for URI forwarding
sleep 8

# Pin the geometry. NOT fullscreen — see below.
WID=$(xdotool search --onlyvisible --name '^RustDesk$' | head -1)
xdotool windowsize "$WID" 1600 950
xdotool windowmove "$WID" 100 40
```

Four things the environment will bite you with:

1. **Set `HOME` and the XDG directories explicitly.** Without them the client
   logs `MissingPlatformDirectoryException: Unable to get application documents
   directory`. It runs anyway, but that is where it persists its configuration
   and saved peers — leave it broken and it may not remember the host between
   restarts, which breaks unattended operation.

2. **Do not make the window fullscreen.** Flutter on Linux reports incorrect
   display information via `MediaQuery` under Xvfb specifically when fullscreen
   ([flutter#162801](https://github.com/flutter/flutter/issues/162801)); the
   documented workaround is to run windowed. The window opens centred at roughly
   800×590, so resize it to a fixed large size and strip the decorations with an
   openbox rule instead — then verify the geometry before recording anything:

   ```xml
   <!-- ~/.config/openbox/rc.xml, inside <applications> -->
   <application name="rustdesk">
     <decor>no</decor>
     <position force="yes"><x>100</x><y>40</y></position>
   </application>
   ```

3. **The remote session opens in a separate top-level window.** The Flutter
   client is multi-window: connecting does not fill the main window, it spawns
   another one. The main window is titled exactly `RustDesk`, the session one
   carries the peer id, so the agent looks for `.+ - RustDesk$` and takes the
   largest match. Override with `--window-name` or `$NPC_WINDOW_NAME`.

4. **Kill any running instance before launching with `--connect`.** RustDesk
   reports "Key mismatch" when `--connect <id> --password <pw>` is used while a
   GUI instance is already running
   ([#13693](https://github.com/rustdesk/rustdesk/issues/13693),
   [#10088](https://github.com/rustdesk/rustdesk/issues/10088)). The `pkill`
   above is part of startup, not something to remember.

### The unattended password is a credential

Prefer connecting **once by hand** through the VNC tunnel and letting the client
remember the peer — that is what the XDG directories are for, and it keeps the
password off every later command line. `--connect ID --password PW` puts it in
`/proc/*/cmdline`, where any user on the box can read it.

If you must automate the connect, keep it in a file with restricted permissions
and read it in:

```bash
chmod 600 ~/.rustdesk-peer          # ID=... and PW=... on two lines
. ~/.rustdesk-peer && rustdesk --connect "$ID" --password "$PW"
```

Never hardcode it in a source file, never write it to the log, and never put it
in a Telegram message or a screenshot caption.

---

## Reaching the VM's display

The operator needs the VM's own desktop by hand: to connect RustDesk the first
time, position the window, and read coordinates. x11vnc, bound to loopback,
reached over an SSH tunnel:

```bash
x11vnc -display :99 -rfbport 5900 -localhost -forever -shared -noxdamage \
       -afteraccept 'touch /tmp/npc-operator-present' \
       -gone        'rm -f /tmp/npc-operator-present'
```

```bash
ssh -N -L 5900:127.0.0.1:5900 npc@vm    # then point a VNC client at 127.0.0.1:5900
```

`-localhost` is what makes SSH the only way in. The lock file it touches now
guards the **VM's own display**, so a debugging connection does not fight the
automation for the cursor — while it exists the loop pauses rather than clicks,
and picks up again when the viewer disconnects. It has nothing to do with the
son's machine.

---

## Setup: recording the coordinates and the reference

More confusing than it sounds, because the target is a remote screen rendered
inside a local window. In order:

1. Connect to the VM's display over the VNC tunnel.
2. Connect RustDesk to the son's machine, size and position the window, then
   leave it alone forever.
3. Get the queue page to the settled "all appointments are currently booked"
   state.
4. Check what the agent can see:

   ```bash
   npc-setup --inspect
   {"status":"ok","display":":99","screen":{"width":1920,"height":1080},
    "windows":[{"id":"4194313","x":100,"y":40,"w":1600,"h":950}],
    "content":{"x":100,"y":90,"w":1600,"h":850},"letterboxed":true}
   ```

5. Hover each button **inside the remote view** and read the position:

   ```bash
   DISPLAY=:99 watch -n0.2 xdotool getmouselocation
   ```

   That is the coordinate on the VM's virtual display, which is what the agent
   clicks. Coordinates from the son's own screen are a different space and will
   not work.

6. Record:

   ```bash
   npc-setup --name booking --book 950,612 --ok 880,540 --threshold 0.05
   ```

Coordinates are stored as **percentages of the content rectangle**, not pixels,
and multiplied by the current size at click time. The content rectangle is the
remote desktop *inside* the window: if RustDesk letterboxes it with black bars,
measuring from the window edge puts every coordinate out by the bar width, so
the bars are detected at setup and recorded alongside the references. If the
detection gets it wrong — a remote desktop that is genuinely black down one
side — override it with `--content X,Y,W,H`.

### Watching a region instead of the whole screen

By default the comparison looks at the whole content rectangle. It can be
narrowed to one rectangle, either explicitly or as a square around a button:

```bash
npc-setup --name booking --book 950,612 --ok 880,540 --region-around ok --radius 200
npc-setup --name booking --book 950,612 --ok 880,540 --region 700,300,600,500
```

Setup reports what it will actually watch, and whether each button falls inside
it:

```json
{"status":"ok","watching":{"x":680,"y":340,"w":400,"h":400},"whole_content":false,
 "region_contains_book":true,"region_contains_ok":true}
```

**Why you might want this.** An ad, a ticker or a clock elsewhere on the page
stops mattering. More importantly, sensitivity goes *up*: the metric is a
fraction of the area being compared, so a small dialog that scores 0.02 against
a whole screen — under any sane threshold — scores an unmissable fraction of a
400×400 square. If the response you are waiting for is a modest dialog, this is
the difference between seeing it and not.

**Why the default is the whole screen.** Anything that happens outside the
region is invisible, *including a slot*. If the site announces one somewhere you
did not think to watch — a list of dates lower down, a banner along the top —
nobody finds out. A false alarm costs one Telegram at 3am; a missed slot costs
the appointment. Narrow it only after you have seen the real page and know where
the answer appears.

Two things to get right:

- **Anchor on the response, not on the button you press.** The "all booked" box
  need not be near BOOK — on [the test page](#a-local-rehearsal-no-vm-no-australia)
  it appears *above* it. `--region-around ok` is usually the better anchor,
  because OK is inside the box that changes.
- **Re-calibrate after changing it.** `npc-calibrate --name <name>` measures the
  noise floor inside the recorded region, and the right threshold for a 400×400
  square is not the right threshold for a 1600×900 one.

The region is stored as fractions of the content rectangle, like the
coordinates, so a change in the rendered scale moves it along with everything
else. Changing the region means re-recording the reference — pass `--force`.

**If the real problem is only ads**, the narrower tool is a noise mask: exclude
the individual tiles that vary between captures of an identical state, and keep
the whole field of view. It is not built. The region is the blunter instrument,
and the blunter instrument is the one that exists.

---

References are written once and **never overwritten automatically**; a
self-healing reference drifts silently onto the wrong screen and you find out
when it clicks something it shouldn't. To re-record, delete
`~/.npc/refs/<name>/` deliberately or pass `--force`.

### Calibrating the threshold

Do not hard-code a guess. With the remote screen settled on the boring state:

```bash
npc-calibrate --name booking --shots 10 --interval 3
{"shots":10,"pairs":45,"max":0.0139,"mean":0.008,"recommended_threshold":0.042,"status":"ok"}
```

It measures the changed-tile fraction between captures of an identical state —
the noise floor, which over RustDesk is codec churn rather than anything on the
page — and suggests three times it. Put that in `--threshold` at setup, or edit
`threshold` in `~/.npc/scenarios/<name>.json`.

Prefer a **fixed image quality** in the RustDesk session too, since it otherwise
varies compression with bandwidth. But do not rely on it: the tolerance and the
three-strike debounce are the real defence, and they have to work regardless,
because compression artefacts are inherent to receiving a screen over a video
codec. The image RustDesk renders is the only source of truth available — there
is no agent on the son's machine, and that is the deliberate cost of him
installing nothing.

---

## Running it

```bash
export NPC_TELEGRAM_TOKEN=123456:AA...
export NPC_TELEGRAM_CHAT_ID=987654321
npc-watch --name booking
```

It refuses to start without both, unless you pass `--no-telegram`. The loop's
whole output is a message to a human; running it unable to send one is exactly
the silent failure this system exists to prevent.

### As services

Four units, started once and left alone. The display stack must survive the SSH
session that started it, or the whole thing dies when you log out.

`/etc/systemd/system/npc-display.service` — Xvfb, the window manager and the
viewer, in one place because they share a lifetime:

```ini
[Unit]
Description=npc virtual display
After=network-online.target

[Service]
User=npc
ExecStart=/bin/bash -c '\
  Xvfb :99 -screen 0 1920x1080x24 +extension GLX +extension RANDR +extension RENDER -noreset & \
  sleep 2; \
  DISPLAY=:99 openbox & \
  DISPLAY=:99 x11vnc -display :99 -rfbport 5900 -localhost -forever -shared -noxdamage \
      -afteraccept "touch /tmp/npc-operator-present" -gone "rm -f /tmp/npc-operator-present"'
Restart=always

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/npc-rustdesk.service` — the client, through the script
above, which is where the environment and the `pkill` live:

```ini
[Unit]
Description=RustDesk client on the virtual display
After=npc-display.service
Requires=npc-display.service

[Service]
User=npc
ExecStart=/home/npc/bin/rustdesk-client.sh
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now npc-display npc-rustdesk
DISPLAY=:99 npc-setup --inspect        # the session window should be listed
```

And the loop itself — `/etc/systemd/system/npc.service`:

```ini
[Unit]
Description=npc appointment watcher
After=network-online.target

[Service]
User=npc
Environment=DISPLAY=:99
Environment=NPC_TELEGRAM_TOKEN=123456:AA...
Environment=NPC_TELEGRAM_CHAT_ID=987654321
ExecStart=/usr/local/bin/npc-watch --name booking
Restart=no
StandardOutput=append:/home/npc/.npc/events.jsonl

[Install]
WantedBy=multi-user.target
```

`Restart=no` on purpose. **Restarting after a stop is manual and should stay
manual**: the operator connects, confirms the remote screen is in the expected
state, and starts the loop again. There is no scheduler and no auto-restart. Put
the token in an `EnvironmentFile=` with mode 600 if the unit file is not private.

Nothing invokes the loop remotely. There is no daemon, no port, no `--scenario`
stdin contract, and no per-run SSH invocation — that was the previous design,
when this drove a browser on a laptop.

### What it prints

One JSON object per line, on stdout, as things happen:

```json
{"time":"...","name":"booking","event":"started","state":"clicking","content":{...},"telegram":true}
{"time":"...","name":"booking","event":"cycle","diff":0.0069,"clicks":42,"strikes":null}
{"time":"...","name":"booking","event":"strike","n":1,"diff":0.1111}
{"time":"...","name":"booking","event":"stopped","reason":"changed","message":"...","diff":0.111,"telegram":true}
{"time":"...","name":"booking","event":"recovering","matches":2,"diff":0.0069}
{"time":"...","name":"booking","event":"resumed","telegram":true}
{"time":"...","name":"booking","event":"paused","message":"an operator is on the VM's display"}
```

Stops, resumes and errors also go to `~/.npc/logs/<name>.log`:

```
2026-08-02T20:40:21+10:00 name=booking status=changed clicks=43 diff=0.111 limit=0.060 message=the screen no longer matches ...
2026-08-02T20:40:39+10:00 name=booking status=resumed clicks=43
2026-08-02T20:40:49+10:00 name=booking status=session clicks=63 message=the RustDesk session window is gone and no rustdesk process is running
```

### The three messages

```
[SLOT?] npc/booking                    + the screenshot
the screen no longer matches the 'all booked' response - a slot may be open.
Take over now.
2026-08-02T20:40:21+10:00
difference 0.111, threshold 0.060
43 clicks so far. Clicking has stopped.
```

```
[DEAD SESSION?] npc/booking            + the screenshot
the RustDesk session window is gone and no rustdesk process is running
...
```

```
[RESUMED] npc/booking                  (no screenshot, so it cannot be
the boring 'all booked' screen came back and stayed for 3 checks.   mistaken
Clicking has resumed on its own - nothing needs doing.              for an alert)
```

The screenshot is the whole virtual display, not just the content rectangle, so
the operator can tell a real slot from a RustDesk dialog before they scramble.
If the upload fails the text is sent on its own — the message matters more than
the picture.

---

## Comparison

Whole screenshots never match. Both images are reduced to 480×270 greyscale and
split into a 16×9 grid of 30×30 tiles; a tile counts as changed when its mean
absolute difference exceeds 12/255, and the result is the fraction of tiles
changed. Roughly 3 ms, against 10–30 ms for the capture itself.

Downsampling to a fixed size is also what makes **scale** a non-issue: if
RustDesk renders the remote desktop larger or smaller, both sides are normalised
to the same grid, as long as the aspect ratio holds. Coordinates survive it
because they are percentages. Neither of those needs pinning in the session.

The measure is relative to whatever is being compared, so against a whole screen
something small but important — an inline error, a changed button label — may not
escalate on its own. That is the reason to calibrate rather than accept the
default, and the reason
[the watched region can be narrowed](#watching-a-region-instead-of-the-whole-screen).

---

## Storage

```
~/.npc/
  scenarios/<name>.json      the two coordinates as percentages, the waits, the threshold
  refs/<name>/boring.png     the "all booked" reference, cropped to the content rectangle
  refs/<name>/meta.json      display size, window geometry, content rectangle, watched region
  logs/<name>.log            stops, resumes and errors
  watch.lock                 held for the life of the loop
```

```json
{
  "book": {"x_pct": 0.53125, "y_pct": 0.672941},
  "ok":   {"x_pct": 0.4875,  "y_pct": 0.529412},
  "threshold": 0.05,
  "book_wait": 3.0,
  "ok_wait": 1.5,
  "recheck_seconds": 4.0,
  "watch_interval": 60.0,
  "debounce": 3
}
```

Three things make stale state refuse to run rather than click blind: the
**display resolution**, the **window geometry** and the **window's existence**.
A moved or resized window is reported and stopped on, not re-located
automatically — re-locating is how a coordinate quietly ends up on the wrong
button.

Two watchers would fight over one cursor, so the loop takes an exclusive
`flock`; a second exits `{"status": "busy"}`. The kernel releases it even if the
process is killed.

Paths and defaults are overridable: `NPC_HOME`, `NPC_OPERATOR_LOCK`,
`NPC_DISPLAY`, `NPC_XAUTHORITY`, `NPC_WINDOW_NAME`, `NPC_TELEGRAM_TOKEN`,
`NPC_TELEGRAM_CHAT_ID`.

---

## A local rehearsal: no VM, no Australia

[docs/index.html](docs/index.html) is the queue page in miniature, for
exercising the loop before any of the above exists.

```bash
python3 -m http.server 8000 -d docs      # then http://localhost:8000
```

- **BOOK** waits one second, then shows a grey box: *все занято*, with a small
  grey **OK**. That is the boring state.
- The unlabelled checkbox in the top-left corner is the switch. With it ticked,
  BOOK shows a green box instead — bigger, higher up, *Вы в очереди*, with a
  large green OK. That is a slot appearing.
- **The same switch, flippable from anywhere:** the page polls
  [docs/state.json](docs/state.json) every five seconds and treats
  `{"slot": true}` exactly like a ticked box. Publish `docs/` with GitHub Pages
  and editing that one file on github.com flips the switch on the son's screen
  without touching his machine. A failed fetch keeps the last known value —
  a network blip must not silently disarm the switch. It needs http(s):
  under `file://` the fetch is blocked and only the checkbox works.
- Nothing on the page hovers, focuses, animates or ticks. Pixels change only
  when the state changes, which is what lets you see the watcher's real noise
  floor rather than the cursor's. The poll draws nothing.

**The reference is the dialog, not the empty page.** The loop screenshots
*after* clicking BOOK and waiting, so record with *все занято* on screen. BOOK
sits below that box and stays clickable; the green box is far enough out — a
different size, colour and position — that no threshold worth using could miss it.

### Standing in for RustDesk

The agent looks for a window matching `.+ - RustDesk$`. Any window can wear that
name, which is the whole trick for rehearsing:

```bash
Xvfb :77 -screen 0 1280x800x24 +extension GLX +extension RANDR +extension RENDER -noreset &
DISPLAY=:77 <something that draws a window> &
WID=$(DISPLAY=:77 xdotool search --onlyvisible --name . | tail -1)
DISPLAY=:77 xdotool set_window --name "918273645 - RustDesk" "$WID"

export NPC_DISPLAY=:77
npc-setup --inspect
npc-setup --name rehearsal --book X,Y --ok X,Y --threshold 0.06
npc-watch --name rehearsal --no-telegram
```

Then cover the window with another one and watch the three strikes land; close
it and watch the loop resume itself; kill the window and watch the dead-session
message. That is exactly how the acceptance behaviours below were verified here,
with `xmessage` as the window.

Two things that will bite on a desktop machine:

- **Use a `.deb` browser, not a snap.** Snap Firefox will not start on a display
  it did not launch with, and refuses a profile outside `$HOME` — observed here,
  silently, with an empty log.
- **Do not point the agent at your own screen.** On a Wayland desktop, capture
  and input injection are both blocked, and even under Xorg the automation would
  be fighting your hands for the cursor. Rehearse on Xvfb.

---

## Acceptance test

1. On the VM, Xvfb is running with the RustDesk client **windowed at a fixed
   size — not fullscreen** — decorations stripped, connected to the son's
   machine. `npc-setup --inspect` reports the window, and a screenshot of `:99`
   shows his desktop.
2. `npc-setup --name booking --book X,Y --ok X,Y` records the reference and both
   coordinates as percentages of the content rectangle.
3. `npc-watch --name booking` clicks BOOK, waits, sees the familiar response,
   clicks OK, and repeats — visibly, for at least twenty cycles.
4. Open any window on the son's machine. After **three consecutive** mismatches
   the loop stops clicking and a Telegram arrives with the screenshot.
5. Restore the "all booked" state. After three consecutive matches the loop
   resumes on its own and a second Telegram says so.
6. Kill the RustDesk connection. A Telegram reports a dead session.

---

## What is verified, and what is not

`selftest.py` covers the whole loop against synthetic frames with the display,
the window, the mouse and Telegram all faked: the comparison and its noise
tolerance, scale normalisation, letterbox detection and its refusal to guess,
percentage coordinates round-tripping to the pixel they were recorded at, a
narrowed region ignoring an ad in the corner while still catching a change
inside it — and **missing** one outside it, which is the cost, asserted rather
than assumed,
one and two mismatches being absorbed and three not, the stop, the auto-resume,
a booking form on screen keeping the loop from resuming, a vanished window and a
moved window, the operator pause, the resolution guard, the busy lock, plan
validation, the multipart upload body, the token never reaching a log line, the
photo-to-text fallback, and the geometry of the mouse path. 44 checks, no display
needed.

Run end to end on a real `Xvfb :77` against a stand-in window renamed to
`918273645 - RustDesk`: setup detected the window and trimmed the content
rectangle, the loop ran **31 cycles / 63 clicks** of BOOK-and-OK, an overlaid
window produced two strikes and then a stop at diff 0.111 against a threshold of
0.06, removing it resumed the loop by itself after three matches, and killing the
window produced the dead-session stop. All six acceptance behaviours, with the
son's machine faked. The Telegram transport was verified against a loopback HTTP
server: a 14 KB PNG uploaded as multipart with the caption and chat id, two
failures retried into a success, and text-only sends as JSON.

**Not verified, and worth doing early on the real VM:** that the Flutter client
renders a *live remote session* at a usable framerate under software rendering.
The client's own UI is known to render on Xvfb with llvmpipe; video decode is
CPU-side VP8/VP9 and should be fine, but confirm it against a real host before
trusting the loop on top of it. If it disappoints, the fallbacks in order are the
`-sciter` build (Cairo/CPU, no GL dependency at all, and single-window so the
multi-window gotcha disappears — but deprecated upstream); a real Xorg with
`xserver-xorg-video-dummy`, which has fuller GLX and DRI plumbing; or TigerVNC's
`Xvnc`, which is also a real X server and lets you watch what is happening
instead of debugging blind through screenshots.

Also unexercised here: x11vnc over an SSH tunnel from another machine, and
Telegram against Telegram's own servers.

---

## Known limitations, accepted rather than solved

- **A RustDesk reconnect looks like a screen change.** There is no logic to tell
  them apart: the debounce absorbs the brief version, the auto-resume recovers
  from the longer one, and you may still get one Telegram out of it. That is the
  right amount of effort to spend here.
- **The Bezier path is resampled by RustDesk's protocol** on the way to
  Australia, so what arrives is coarser than what was drawn. The curve and the
  jittered timing cost nothing and stay; nothing is done to preserve path
  fidelity across the wire.
- **Everything depends on the window staying put** and on the son's screen
  resolution not changing. Both are guarded, neither is repaired.

## Not implemented, on purpose

No VLM, no OCR, no element detection, no keyboard input, no scenario recorder,
no self-healing references, no database, no retry logic beyond the Telegram
transport, no resume. Two buttons at two fixed coordinates is the whole job.

Anti-detection is gone from the design entirely: the browser is the son's real
browser, on his real machine, on his real connection. There is nothing to
engineer.
