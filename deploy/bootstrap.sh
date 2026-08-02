#!/usr/bin/env bash
# One-shot bootstrap for a fresh Debian 12 / Ubuntu VM in australia-southeast1.
#
# Run it from a checkout, as an ordinary user with sudo:
#
#     git clone <repo> ~/simple && ~/simple/deploy/bootstrap.sh
#
# It installs the packages, the RustDesk client and this program, writes the
# display and client services, and starts them. It deliberately does NOT start
# the watch loop: that needs a human to connect RustDesk to the peer, pin the
# window and record the reference first, and no amount of scripting can do
# those without seeing the son's screen.
set -euo pipefail

RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$RUN_HOME/bin"
VENV="$RUN_HOME/.npc-venv"
DISPLAY_NUM="${NPC_DISPLAY:-:99}"
RUSTDESK_VERSION="${RUSTDESK_VERSION:-1.4.9}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mbootstrap: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] && [ -z "${SUDO_USER:-}" ] && \
    die "run this as an ordinary user with sudo, not as root: the services and
     the RustDesk config belong to a login user with a real home directory"

# --- packages ---------------------------------------------------------------
say "packages"
sudo apt-get update -qq
# x11-utils carries xprop, which is how the fullscreen state is read; wmctrl is
# the only way to clear it. Both were missing from the first install here and
# both failures are silent, which is why they are pinned in this list.
sudo apt-get install -y -qq \
    xvfb openbox x11vnc xdotool wmctrl x11-utils mesa-utils dbus-x11 \
    libgl1-mesa-dri libglx-mesa0 libegl-mesa0 \
    python3-venv python3-tk curl

# --- RustDesk ---------------------------------------------------------------
say "RustDesk $RUSTDESK_VERSION"
if ! command -v rustdesk >/dev/null; then
    deb="/tmp/rustdesk-${RUSTDESK_VERSION}-x86_64.deb"
    curl -fsSL -o "$deb" \
        "https://github.com/rustdesk/rustdesk/releases/download/${RUSTDESK_VERSION}/rustdesk-${RUSTDESK_VERSION}-x86_64.deb"
    sudo apt-get install -y -qq "$deb"
    rm -f "$deb"
fi

# The .deb installs and starts rustdesk.service, which makes this VM a remote
# control *host*. It is not one - it is a client - and leaving it running means
# two instances sharing one config directory, which is how "Key mismatch"
# starts. Stop it before anything else touches RustDesk.
sudo systemctl disable --now rustdesk 2>/dev/null || true

# --- this program -----------------------------------------------------------
say "npc"
sudo -u "$RUN_USER" python3 -m venv "$VENV"
sudo -u "$RUN_USER" "$VENV/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$VENV/bin/pip" install -q "$REPO"
for tool in npc-setup npc-watch npc-calibrate; do
    sudo ln -sf "$VENV/bin/$tool" "/usr/local/bin/$tool"
done
sudo -u "$RUN_USER" "$VENV/bin/python" "$REPO/selftest.py" >/dev/null || \
    die "selftest failed - fix that before going near a real screen"

# --- scripts and the openbox rule -------------------------------------------
say "scripts"
sudo -u "$RUN_USER" mkdir -p "$BIN" "$RUN_HOME/.config/openbox"
for script in display.sh rustdesk-client.sh pin-window.sh; do
    sudo install -m 755 -o "$RUN_USER" "$REPO/deploy/$script" "$BIN/$script"
done

# Decorations are stripped by rule rather than by making the window fullscreen:
# fullscreen is what makes Flutter report wrong display metrics under Xvfb
# (flutter#162801) and what makes openbox ignore every resize.
if [ ! -f "$RUN_HOME/.config/openbox/rc.xml" ]; then
    sudo -u "$RUN_USER" tee "$RUN_HOME/.config/openbox/rc.xml" >/dev/null <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <applications>
    <application name="rustdesk">
      <decor>no</decor>
      <maximized>no</maximized>
    </application>
  </applications>
</openbox_config>
XML
fi

# --- services ---------------------------------------------------------------
say "services"
sudo tee /etc/systemd/system/npc-display.service >/dev/null <<UNIT
[Unit]
Description=npc virtual display (Xvfb, openbox, x11vnc)
After=network-online.target

[Service]
User=$RUN_USER
Environment=NPC_DISPLAY=$DISPLAY_NUM
ExecStart=$BIN/display.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# RuntimeDirectory gives the client a writable XDG_RUNTIME_DIR without needing
# a login session, which a system service does not have.
sudo tee /etc/systemd/system/npc-rustdesk.service >/dev/null <<UNIT
[Unit]
Description=RustDesk client on the virtual display
After=npc-display.service
Requires=npc-display.service

[Service]
User=$RUN_USER
RuntimeDirectory=npc
Environment=NPC_DISPLAY=$DISPLAY_NUM
Environment=XDG_RUNTIME_DIR=/run/npc
ExecStart=$BIN/rustdesk-client.sh
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT

# Restart=no on purpose. Restarting after a stop is manual and stays manual:
# the operator connects, confirms the remote screen is in the expected state,
# and starts the loop again. A scheduler here would click into whatever the
# screen happened to show.
sudo tee /etc/systemd/system/npc.service >/dev/null <<UNIT
[Unit]
Description=npc appointment watcher
After=npc-rustdesk.service

[Service]
User=$RUN_USER
Environment=DISPLAY=$DISPLAY_NUM
EnvironmentFile=/etc/npc.env
ExecStart=/usr/local/bin/npc-watch --name booking
Restart=no
StandardOutput=append:$RUN_HOME/.npc/events.jsonl
StandardError=append:$RUN_HOME/.npc/events.jsonl

[Install]
WantedBy=multi-user.target
UNIT

# The token is a credential: it never belongs in a unit file that is world
# readable, and never in the log.
if [ ! -f /etc/npc.env ]; then
    sudo tee /etc/npc.env >/dev/null <<'ENV'
NPC_TELEGRAM_TOKEN=
NPC_TELEGRAM_CHAT_ID=
ENV
fi
sudo chmod 600 /etc/npc.env
sudo -u "$RUN_USER" mkdir -p "$RUN_HOME/.npc"

sudo systemctl daemon-reload
sudo systemctl enable --now npc-display npc-rustdesk

# --- the one check that decides everything ----------------------------------
say "Mesa"
sleep 5
renderer="$(DISPLAY=$DISPLAY_NUM glxinfo -B 2>/dev/null | grep -i 'OpenGL renderer' || true)"
echo "${renderer:-(no answer from glxinfo)}"
case "$renderer" in
    *llvmpipe*) ;;
    *) die "Mesa is not reporting llvmpipe. Nothing else will work and no
     environment variable will rescue it - check that Xvfb started with
     +extension GLX and that libgl1-mesa-dri is installed." ;;
esac

cat <<NEXT

$(printf '\033[1mInstalled.\033[0m') What is left needs eyes on the son's screen:

  1. From your laptop:   gcloud compute ssh $(hostname) --zone=australia-southeast1-b -- -N -L 5900:127.0.0.1:5900
     then point a VNC client at 127.0.0.1:5900

  2. In that VNC window, connect RustDesk to the peer by hand and tick
     "remember password". Doing it by hand keeps the password out of
     /proc/*/cmdline, where --connect --password would put it.

  3. $BIN/pin-window.sh          # clears fullscreen, pins 1600x950+100+40, verifies

  4. Get the queue page to its "all booked" state, then:
       npc-setup --inspect
       DISPLAY=$DISPLAY_NUM watch -n0.2 xdotool getmouselocation   # hover BOOK, then OK
       npc-setup --name booking --book X,Y --ok X,Y --region-around ok --radius 200
       npc-calibrate --name booking --shots 10 --interval 3

  5. Put the recommended threshold in ~/.npc/scenarios/booking.json,
     the bot token and chat id in /etc/npc.env, then:
       sudo systemctl start npc

  Disconnect the VNC viewer when you are done: while it is attached the loop
  pauses rather than fighting you for the cursor.
NEXT
