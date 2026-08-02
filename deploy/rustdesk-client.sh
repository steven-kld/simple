#!/usr/bin/env bash
# Start the RustDesk client on the virtual display. Every line earns its place.
set -euo pipefail

# x11vnc and GTK both look at WAYLAND_DISPLAY and act on it even when pointed
# at an X display: x11vnc exits outright ("Wayland display server detected"),
# GTK quietly prefers Wayland over :99. A GCP VM has neither, but a laptop
# standing in for one does, and the failure is confusing enough to be worth
# one line here.
unset WAYLAND_DISPLAY XDG_SESSION_TYPE

export DISPLAY="${NPC_DISPLAY:-:99}"
# Without HOME and the XDG directories the client logs
# MissingPlatformDirectoryException and forgets its saved peers between
# restarts, which is precisely what unattended operation cannot survive.
export HOME="${HOME:-/home/$(id -un)}"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export LIBGL_ALWAYS_SOFTWARE=1     # RustDesk's own allow-always-software-render
export GALLIUM_DRIVER=llvmpipe     # insurance if driver selection misbehaves
export GDK_BACKEND=x11

mkdir -p "$XDG_RUNTIME_DIR" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"
chmod 700 "$XDG_RUNTIME_DIR" || true

# RustDesk reports "Key mismatch" when a second instance starts while one is
# running (rustdesk#13693, #10088). Cleanup is part of startup, not something
# to remember.
pkill -x rustdesk || true
sleep 2

dbus-run-session -- rustdesk &
client=$!

# The session window only exists once someone connects the client to the peer
# by hand, so a failure to pin is not a failure to run. Report and carry on.
"$(dirname "$0")/pin-window.sh" || \
    echo "rustdesk-client: not pinned yet - connect to the peer, then run pin-window.sh" >&2

wait "$client"
