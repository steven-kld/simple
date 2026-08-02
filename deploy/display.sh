#!/usr/bin/env bash
# Xvfb, the window manager and the viewer share a lifetime, so they share a
# unit: kill any one of them and every recorded coordinate is meaningless.
set -euo pipefail

# x11vnc refuses to start when WAYLAND_DISPLAY is set in its environment, even
# though it is pointed at an X display: "Wayland display server detected ...
# Exiting". It never reads the variable's value, only its presence.
unset WAYLAND_DISPLAY XDG_SESSION_TYPE

DISPLAY_NUM="${NPC_DISPLAY:-:99}"
GEOMETRY="${NPC_SCREEN:-1920x1080x24}"
LOCK="${NPC_OPERATOR_LOCK:-/tmp/npc-operator-present}"

# The extensions are named rather than trusted to defaults: without GLX the
# Mesa check below fails and RustDesk never draws.
Xvfb "$DISPLAY_NUM" -screen 0 "$GEOMETRY" \
    +extension GLX +extension RANDR +extension RENDER -noreset &
xvfb=$!
sleep 2

export DISPLAY="$DISPLAY_NUM"
# RustDesk needs focus handling; its dialogs misbehave without a window manager.
openbox &

# -localhost is what makes the SSH tunnel the only way in. The lock file guards
# the VM's *own* display, so a debugging connection does not fight the
# automation for the cursor: while it exists the loop pauses rather than clicks.
x11vnc -display "$DISPLAY_NUM" -rfbport 5900 -localhost -forever -shared \
    -noxdamage -nopw \
    -afteraccept "touch $LOCK" \
    -gone "rm -f $LOCK" &

wait "$xvfb"
