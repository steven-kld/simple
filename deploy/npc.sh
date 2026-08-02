#!/usr/bin/env bash
# The whole system, up or down, on any systemd Linux box.
#
#   ./deploy/npc.sh up       install if needed, start everything, say what is missing
#   ./deploy/npc.sh down     stop everything
#   ./deploy/npc.sh status   what is running, what it sees
#   ./deploy/npc.sh logs     follow the loop
#
# `up` is idempotent and safe to re-run: it installs only what is absent and
# starts only what is stopped.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DISPLAY_NUM="${NPC_DISPLAY:-:99}"
NAME="${NPC_NAME:-booking}"
UNITS=(npc-display npc-rustdesk)
HOME_DIR="${NPC_HOME:-$HOME/.npc}"
ENV_FILE=/etc/npc.env

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

command -v systemctl >/dev/null || die "npc.sh needs systemd; without it, run deploy/display.sh
and deploy/rustdesk-client.sh by hand and keep the shell open."

installed() { [ -f /etc/systemd/system/npc-display.service ]; }
active()    { systemctl is-active --quiet "$1"; }

# The loop cannot start before a human has connected RustDesk to the peer and
# recorded the boring reference against it. Nothing here can do that, so `up`
# reports what is missing instead of pretending.
ready_to_watch() {
    [ -f "$HOME_DIR/refs/$NAME/boring.png" ] || return 1
    sudo grep -q '^NPC_TELEGRAM_TOKEN=.\+' "$ENV_FILE" 2>/dev/null || return 1
    sudo grep -q '^NPC_TELEGRAM_CHAT_ID=.\+' "$ENV_FILE" 2>/dev/null || return 1
}

case "${1:-up}" in

up)
    installed || { bold "installing"; "$REPO/deploy/bootstrap.sh"; }

    bold "starting the display and the client"
    # enable, not just start: on a server the point is surviving a reboot.
    sudo systemctl enable --quiet --now "${UNITS[@]}"
    sleep 5

    # The check that decides everything. No environment variable rescues a
    # missing llvmpipe, so fail here rather than three steps later.
    renderer="$(DISPLAY=$DISPLAY_NUM glxinfo -B 2>/dev/null | grep -i 'OpenGL renderer' || true)"
    case "$renderer" in
        *llvmpipe*) echo "  ${renderer# }" ;;
        *) die "Mesa is not reporting llvmpipe (got: ${renderer:-nothing}).
Check that Xvfb started with +extension GLX and that libgl1-mesa-dri is installed:
  journalctl -u npc-display -n 40" ;;
    esac

    if ready_to_watch; then
        bold "starting the loop"
        sudo systemctl start npc
        echo "  watching '$NAME'. Follow it with: $0 logs"
    else
        warn "
The display and the client are up. The loop is not, and cannot be yet -
it needs a reference recorded against the peer's real screen:

  1. ssh -N -L 5900:127.0.0.1:5900 $(id -un)@$(hostname -f 2>/dev/null || hostname)
     then point a VNC client at 127.0.0.1:5900

  2. In that window, connect RustDesk to the peer by hand and tick
     'remember password' - by hand keeps the password out of /proc/*/cmdline,
     where --connect --password would put it.

  3. $HOME/bin/pin-window.sh

  4. With the page settled on its boring state:
       npc-setup --inspect
       DISPLAY=$DISPLAY_NUM watch -n0.2 xdotool getmouselocation   # hover BOOK, then OK
       npc-setup --name $NAME --book X,Y --ok X,Y --region-around ok --radius 200
       npc-calibrate --name $NAME --shots 10 --interval 3

  5. Put the recommended threshold in $HOME_DIR/scenarios/$NAME.json and the
     bot token in $ENV_FILE, then run '$0 up' again.

Disconnect the VNC viewer when you are done: while it is attached the loop
pauses rather than fighting you for the cursor."
    fi
    ;;

down)
    bold "stopping"
    # The loop first: it must not click into a window that is being torn down.
    sudo systemctl stop npc 2>/dev/null || true
    sudo systemctl disable --quiet --now "${UNITS[@]}" 2>/dev/null || true
    sudo rm -f "${NPC_OPERATOR_LOCK:-/tmp/npc-operator-present}"
    for unit in npc "${UNITS[@]}"; do
        printf '  %-14s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
    done
    ;;

status)
    installed || die "not installed; run '$0 up'"
    for unit in npc-display npc-rustdesk npc; do
        printf '  %-14s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
    done
    echo
    DISPLAY=$DISPLAY_NUM npc-setup --inspect 2>&1 || true
    echo
    tail -n 5 "$HOME_DIR/logs/$NAME.log" 2>/dev/null || echo "  (no log yet)"
    ;;

logs)
    tail -n 40 -f "$HOME_DIR/events.jsonl" 2>/dev/null || \
        journalctl -u npc -f
    ;;

*)
    die "usage: $0 [up|down|status|logs]"
    ;;
esac
