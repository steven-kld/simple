#!/usr/bin/env bash
# Install everything on a Debian or Ubuntu box. Idempotent: safe to re-run.
#
# You do not normally call this directly - `deploy/npc.sh up` runs it when the
# services are missing. It installs and writes; starting and stopping belong to
# npc.sh, so there is exactly one place that decides what is running.
set -euo pipefail

command -v apt-get >/dev/null || {
    echo "bootstrap: this installer is apt-only (Debian, Ubuntu)." >&2
    echo "bootstrap: on another distro install the equivalents of" >&2
    echo "  xvfb openbox x11vnc xdotool wmctrl x11-utils mesa-utils dbus-x11" >&2
    echo "  libgl1-mesa-dri libglx-mesa0 libegl-mesa0 python3-venv python3-tk" >&2
    echo "plus the RustDesk 1.4.9 Flutter client, then re-run npc.sh up." >&2
    exit 1
}

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
echo "bootstrap: installed. Services written, nothing started - that is npc.sh up."
