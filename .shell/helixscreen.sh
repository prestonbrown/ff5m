#!/bin/bash

## HelixScreen service control for the AD5X rig
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license.
##
## HelixScreen is a MIPS buildroot binary, so it runs INSIDE the Forge-X
## chroot ($MOD), whose glibc it was linked against; the stock host rootfs
## cannot load it. init_buildroot bind-mounts /opt/config into the chroot at
## the same path, so the payload below is one tree addressable from both
## sides. init_chroot shares /dev and /tmp with the host, which is what gives
## the UI /dev/fb0, the touchscreen evdev nodes, and a ctl socket the host
## can reach.
##
## A chroot does not isolate PIDs: the UI and its supervisor are host
## processes, so pidof/killall work from here without entering the chroot.
## Only the exec needs it.
##
## The payload is synced, not installed: sync.sh manages everything under
## /opt/config/mod, so an update is a sync plus "helixscreen.sh restart".

source /opt/config/mod/.shell/common.sh

HELIX_ROOT="$MOD_ROOT/.bin/helixscreen"
LAUNCHER="$HELIX_ROOT/bin/helix-launcher.sh"
HOOKS="$HELIX_ROOT/platform/hooks.sh"

DATA_ROOT=/opt/config/mod_data/helixscreen
CONFIG_DIR="$DATA_ROOT/config"
RUN_DIR=/opt/config/mod_data/run
PID_FILE="$RUN_DIR/helixscreen.pid"

## Launcher's own stderr stream ([helix-launcher] lines, crash aborts). The
## app log is helix.log next to it; the names must stay separate so the two
## writers never interleave in one file.
LAUNCHER_LOG=/opt/config/mod_data/log/helixscreen.log

## One-file off switch: present = act like the payload is not installed and
## let the caller fall back to the status card. Cheaper than moving the
## payload away to A/B the panel.
DISABLE_F=/opt/config/mod_data/helixscreen.off

helix_running() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && return 0
    fi
    pidof helix-screen > /dev/null 2>&1
}

helix_available() {
    [ -x "$LAUNCHER" ] && [ -x "$HELIX_ROOT/bin/helix-screen" ]
}

## Seed the durable config dir with the shipped preset on first boot. The app
## reads and writes settings under HELIX_CONFIG_DIR (helixscreen.env), which
## lives outside the payload so a redeploy never resets the UI's state.
seed_config() {
    [ -f "$CONFIG_DIR/settings.json" ] && return 0
    mkdir -p "$CONFIG_DIR"
    if [ -f "$HELIX_ROOT/config/settings.json" ]; then
        cp "$HELIX_ROOT/config/settings.json" "$CONFIG_DIR/settings.json"
        echo "// [helixscreen] Seeded config from the ad5x preset"
    fi
}

start() {
    if [ -f "$DISABLE_F" ]; then
        echo "// [helixscreen] Disabled ($DISABLE_F present)"
        return 1
    fi
    if ! helix_available; then
        echo "// [helixscreen] Payload missing at $HELIX_ROOT"
        return 1
    fi
    if helix_running; then
        echo "// [helixscreen] Already running (pid $(pidof helix-screen))"
        return 0
    fi

    ## The chroot mounts belong to init_buildroot; a manual start after
    ## dispose_chroot would otherwise exec into a rootfs with no /dev.
    if ! mount | grep -q -- " $MOD/dev "; then
        echo "@@ [helixscreen] Chroot is not mounted at $MOD"
        return 1
    fi

    seed_config
    mkdir -p "$RUN_DIR" /opt/config/mod_data/log

    echo "// [helixscreen] Starting..."
    # HELIX_ROOT is a host path ($MOD_ROOT is /usr/data/config/mod here);
    # inside the chroot the same tree is only visible as /opt/config, where
    # init_buildroot binds it. cd to that path, not the host one.
    chroot "$MOD" /bin/sh -c \
        "cd /opt/config/mod/.bin/helixscreen && exec ./bin/helix-launcher.sh" \
        >> "$LAUNCHER_LOG" 2>&1 &
    local launcher_pid=$!
    echo "$launcher_pid" > "$PID_FILE"

    local attempts=0
    while [ "$attempts" -lt 10 ]; do
        if kill -0 "$launcher_pid" 2>/dev/null && pidof helix-screen > /dev/null 2>&1; then
            echo "// [helixscreen] Running (launcher $launcher_pid, ui $(pidof helix-screen))"
            return 0
        fi
        if ! kill -0 "$launcher_pid" 2>/dev/null; then
            break
        fi
        sleep 1
        attempts=$((attempts + 1))
    done

    rm -f "$PID_FILE"
    echo "@@ [helixscreen] Did not come up; see $LAUNCHER_LOG and /opt/config/mod_data/log/helix.log"
    return 1
}

stop() {
    if ! helix_running; then
        echo "// [helixscreen] Not running"
        rm -f "$PID_FILE"
        return 0
    fi

    echo "// [helixscreen] Stopping..."
    ## Signal the watchdog BEFORE the launcher: the launcher shell blocks in
    ## its foreground child and runs no trap until that child exits, so a
    ## TERM straight at it just sits there until the timeout (helixscreen's
    ## own SysV template learned this on a Centauri Carbon). With the
    ## watchdog gone the launcher unblocks and cleans up after itself.
    killall helix-watchdog 2> /dev/null
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        [ -n "$pid" ] && kill "$pid" 2> /dev/null
    fi

    local attempts=0
    while pidof helix-screen > /dev/null 2>&1 && [ "$attempts" -lt 10 ]; do
        sleep 1
        attempts=$((attempts + 1))
    done

    killall helix-screen helix-splash helix-watchdog 2> /dev/null
    sleep 1
    killall -9 helix-screen helix-splash helix-watchdog 2> /dev/null
    rm -f "$PID_FILE"

    if [ -f "$HOOKS" ]; then
        (
            # shellcheck disable=SC1090
            . "$HOOKS"
            command -v platform_post_stop > /dev/null && platform_post_stop
        ) 2> /dev/null
    fi
    echo "// [helixscreen] Stopped"
    return 0
}

status() {
    if helix_running; then
        echo "running (pid $(pidof helix-screen))"
        return 0
    fi
    echo "stopped"
    return 1
}

logs() {
    tail -n "${2:-50}" "/opt/config/mod_data/log/${1:-helix}.log" 2> /dev/null
}

case "$1" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    status)  status ;;
    log)     logs "$2" "$3" ;;
    disable) mkdir -p "$DATA_ROOT"; touch "$DISABLE_F"; echo "// [helixscreen] Disabled" ;;
    enable)  rm -f "$DISABLE_F"; echo "// [helixscreen] Enabled" ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|log [helix|helixscreen] [lines]|disable|enable}"
        exit 1
esac
