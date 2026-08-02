#!/usr/bin/env bash
# Pin the RustDesk session window, and refuse to lie about having done it.
#
# Two traps live here, both found the hard way:
#
#   1. RustDesk opens the session in a SEPARATE top-level window, some seconds
#      after the client starts and only once the peer accepts. Waiting for it
#      is the normal case, not an error path.
#   2. That window arrives with _NET_WM_STATE_FULLSCREEN set, and openbox
#      silently ignores `xdotool windowsize` while it is. Without wmctrl the
#      resize appears to succeed and changes nothing - which is worse than
#      failing, because every coordinate is then recorded against a geometry
#      that will not hold. Fullscreen also makes Flutter report wrong display
#      metrics under Xvfb (flutter#162801).
#
# Usage: pin-window.sh [W] [H] [X] [Y]
set -euo pipefail

export DISPLAY="${NPC_DISPLAY:-:99}"
W="${1:-1600}"; H="${2:-950}"; X="${3:-100}"; Y="${4:-40}"
PATTERN="${NPC_WINDOW_NAME:- - RustDesk$}"

wid=""
for _ in $(seq 90); do
    wid="$(xdotool search --onlyvisible --name "$PATTERN" 2>/dev/null | tail -1 || true)"
    [ -n "$wid" ] && break
    sleep 2
done

if [ -z "$wid" ]; then
    echo "pin-window: no window matching '$PATTERN' on $DISPLAY after 3 minutes." >&2
    echo "pin-window: connect RustDesk to the host by hand over the VNC tunnel first." >&2
    exit 1
fi

wmctrl -i -r "$wid" -b remove,fullscreen
sleep 1
xdotool windowsize "$wid" "$W" "$H"
xdotool windowmove "$wid" "$X" "$Y"
sleep 1

# Verify rather than assume. This is the whole point of the script.
read -r gx gy gw gh <<<"$(xdotool getwindowgeometry --shell "$wid" |
    awk -F= '/^X=/{x=$2} /^Y=/{y=$2} /^WIDTH=/{w=$2} /^HEIGHT=/{h=$2} END{print x,y,w,h}')"

if [ "$gw" != "$W" ] || [ "$gh" != "$H" ] || [ "$gx" != "$X" ] || [ "$gy" != "$Y" ]; then
    echo "pin-window: asked for ${W}x${H}+${X}+${Y}, got ${gw}x${gh}+${gx}+${gy}." >&2
    echo "pin-window: state is now: $(xprop -id "$wid" _NET_WM_STATE)" >&2
    exit 1
fi

echo "pin-window: window $wid pinned at ${gw}x${gh}+${gx}+${gy}"
