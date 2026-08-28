#!/bin/sh
# shellcheck shell=bash
# forge-x bash trampoline. This is a bash script, but the AD5X stock host ships
# no /bin/bash (read-only squashfs, BusyBox), so a #!/bin/bash shebang cannot
# start it. Run under #!/bin/sh and, when bash is absent, re-exec under the
# mod's own bash via the rootfs loader (FORGEX_BASH_* from platform.sh). A no-op
# indirection where /bin/bash exists (AD5M: one extra exec), and skipped when
# the file is sourced (the test harness sources it).
if [ "${_FORGEX_BASHED:-}" != "$0" ] && { [ -z "${BASH_SOURCE:-}" ] || [ "${BASH_SOURCE:-}" = "$0" ]; }; then
    _FORGEX_BASHED="$0"; export _FORGEX_BASHED
    if command -v bash >/dev/null 2>&1; then exec bash "$0" "$@"; fi
    _fx_dir=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
    if [ -f "$_fx_dir/platform.sh" ]; then _fx_p="$_fx_dir/platform.sh"; else _fx_p="$_fx_dir/../platform.sh"; fi
    # shellcheck source=/dev/null
    . "$_fx_p"
    exec "$FORGEX_BASH_LD" --library-path "$FORGEX_BASH_LIBPATH" "$FORGEX_BASH_BIN" "$0" "$@"
fi

## AD5X headless boot bootstrap
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

## The AD5X boots one monolithic stock /usr/prog/app_startup.sh off a read-only
## squashfs; nothing auto-runs /etc/init.d, so Forge-X's S00init -> S99root
## chain never fires the way it does on the AD5M. This script is the single hook
## we inject into that stock file to drive Forge-X's orchestration imperatively,
## calling only the MIPS-safe stages. S55boot/boot_mcu/netd/init_swap/tone.py
## are deliberately never invoked: they are ARM ELFs. zstart_klipper IS invoked
## (Step 7): stock app_startup.sh carries no klipper launch on this board - the
## Qt UI does it, as "/usr/prog/klipper/start.sh &" - so once Step 2 stops that
## UI, nothing else would start klippy.
##
## Subcommands:
##   install    inject the hook into stock app_startup.sh (idempotent, reversible)
##   uninstall  remove the hook / restore the .orig backup (idempotent)
##   run        (default) the ordered headless bring-up; --dry-run prints the plan
##
## The bring-up is bounded by a watchdog (AD5X_BRINGUP_LIMIT, default 180s): it
## runs inside stock app_startup.sh, so an unbounded hang here takes the whole
## printer down with no screen and no network. See watchdog_arm().

# Resolve our own directory so the sibling descriptor/lib scripts are found both
# on-device (executed) and when the test suite sources this file (bash only).
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Absolute stock file the hook is injected into. Overridable so tests can point
# at a fixture instead of the real path.
APP_STARTUP="${AD5X_APP_STARTUP:-/usr/prog/app_startup.sh}"

# The exact line injected into stock app_startup.sh, placed before its
# klipper/start.sh invocation. The path is the on-rig $MOD_ROOT literal: it is
# baked into a STOCK file that cannot source our platform descriptor. The
# trailing "# forge-x" is the idempotency sentinel.
HOOK_LINE='[ -x /usr/data/config/mod/.shell/ad5x_bootstrap.sh ] && /usr/data/config/mod/.shell/ad5x_bootstrap.sh   # forge-x'
HOOK_MARKER='# forge-x'

# ---------------------------------------------------------------------------
# install / uninstall - stock app_startup.sh hook management
# ---------------------------------------------------------------------------

install_hook() {
    local target="$APP_STARTUP"

    if [ ! -f "$target" ]; then
        echo "ad5x_bootstrap: install: target not found: $target" >&2
        return 1
    fi

    # Already injected? idempotent no-op.
    if grep -Fq -- "$HOOK_MARKER" "$target"; then
        echo "ad5x_bootstrap: hook already present in $target"
        return 0
    fi

    # Keep a pristine copy for a byte-identical restore. Never overwrite an
    # existing .orig, so a second install cannot clobber the true original.
    if [ ! -e "$target.orig" ]; then
        cp "$target" "$target.orig"
    fi

    local tmp
    tmp="$(mktemp)"
    if grep -Fq -- 'klipper/start.sh' "$target"; then
        # Insert the hook immediately before the first stock klipper launch, so
        # patches and config land before klippy starts.
        awk -v hook="$HOOK_LINE" '
            (!inserted && index($0, "klipper/start.sh")) { print hook; inserted = 1 }
            { print }
        ' "$target" > "$tmp"
    else
        # No stock klipper launch found: append at the end.
        cat "$target" > "$tmp"
        printf '%s\n' "$HOOK_LINE" >> "$tmp"
    fi
    # "cat >" rewrites in place, preserving the target's mode/inode (a plain mv
    # of the mktemp file would not).
    cat "$tmp" > "$target"
    rm -f "$tmp"

    echo "ad5x_bootstrap: hook installed in $target"
}

uninstall_hook() {
    local target="$APP_STARTUP"

    if [ ! -f "$target" ]; then
        echo "ad5x_bootstrap: uninstall: target not found: $target" >&2
        return 1
    fi

    # Prefer restoring the pristine copy: guarantees byte-identical output.
    if [ -e "$target.orig" ]; then
        cat "$target.orig" > "$target"
        rm -f "$target.orig"
        echo "ad5x_bootstrap: restored $target from .orig"
        return 0
    fi

    # No backup: strip exactly the injected (sentinel-marked) line if present.
    # No-op otherwise, so uninstall is safe to run on a never-installed file.
    if grep -Fq -- "$HOOK_MARKER" "$target"; then
        local tmp
        tmp="$(mktemp)"
        grep -Fv -- "$HOOK_MARKER" "$target" > "$tmp"
        cat "$tmp" > "$target"
        rm -f "$tmp"
        echo "ad5x_bootstrap: removed hook from $target"
    else
        echo "ad5x_bootstrap: hook not present in $target"
    fi
}

# ---------------------------------------------------------------------------
# run - ordered headless bring-up
# ---------------------------------------------------------------------------

# Bind-mount $src at $mnt unless $mnt is already a mountpoint. Idempotent.
_ensure_bind() {
    local src="$1" mnt="$2"
    mkdir -p "$mnt" 2>/dev/null
    if awk -v m="$mnt" '$2 == m { found = 1 } END { exit !found }' /proc/mounts 2>/dev/null; then
        return 0
    fi
    mount --bind "$src" "$mnt"
}

# Set $key=$val in variables.cfg only when the key is absent, so a user's
# explicit choice - and every other key - is left untouched. A non-numeric value
# is written as a Python string literal (quoted): variables.cfg is also read by
# Klipper's [mod_params], which parses each value with ast.literal_eval, so a
# bare enum name like HEADLESS is a syntax error there and halts klippy at
# config load. zconf.sh --get strips the quotes again for shell readers
# (zdisplay.sh etc.), so both consumers agree on the value.
_ensure_var() {
    local key="$1" val="$2" cur
    cur="$("$CMDS"/zconf.sh "$VAR_PATH" --get "$key" "__MISSING__")"
    if [ "$cur" = "__MISSING__" ]; then
        case "$val" in
            ''|*[!0-9]*) val="'$val'" ;;
        esac
        "$CMDS"/zconf.sh "$VAR_PATH" --set "$key=$val"
    fi
}

# ---------------------------------------------------------------------------
# failsafe - one-shot boot-failure recovery (the AD5X has no screen)
# ---------------------------------------------------------------------------
#
# The mod arms BOOT_FLAG_FAILURE before doing any work and clears it only once
# the bring-up finishes. A boot that hangs or crashes in between leaves the flag
# set, so the NEXT boot stands the mod down once and lets stock app_startup.sh
# come up clean - self-recovery with no screen and no key press. BOOT_FLAG_SKIP
# is the explicit one-shot skip (set over USB, a macro, or by hand). Both flags
# live in $MOD_ROOT on the writable data partition (paths from common.sh), so
# they survive the power cycle that recovers from a bad boot.

# Return 0 (and clear the offending flag) when the mod should be skipped.
failsafe_should_skip() {
    if [ -f "$BOOT_FAILURE_F" ]; then
        echo "// [ad5x] previous boot did not complete - skipping the mod once"
        rm -f "$BOOT_FAILURE_F"
        return 0
    fi
    if [ -f "$BOOT_SKIP_F" ]; then
        echo "// [ad5x] skip requested - skipping the mod once"
        rm -f "$BOOT_SKIP_F"
        return 0
    fi
    return 1
}

failsafe_arm() {
    mkdir -p "$(dirname "$BOOT_FAILURE_F")" 2>/dev/null
    : > "$BOOT_FAILURE_F"
}

# ---------------------------------------------------------------------------
# watchdog - bound the bring-up so it can never wedge the boot
# ---------------------------------------------------------------------------
#
# Steps 2-8 run SYNCHRONOUSLY inside stock app_startup.sh. On the AD5X that
# file carries no klipper launch to sit ahead of, so the hook lands at the end -
# the same place ZMOD appends its own prepare.sh. Nothing stock runs after it,
# but a hang there still stops the entire printer - no UI past "Initializing...", no Moonraker, and no
# dropbear either, since that is a mod service too. The machine is then only
# recoverable over USB.
#
# The BOOT_FLAG_FAILURE failsafe does not cover this: it is only consulted on
# the NEXT boot, and it is armed after the bash trampoline and four library
# sources, so anything that hangs earlier is never caught by it at all.
#
# So: arm a timer that kills us if the bring-up overruns. A timeout leaves
# BOOT_FLAG_FAILURE set (only a completed run clears it), so the next boot
# stands the mod down by itself and comes up clean stock.
BRINGUP_LIMIT="${AD5X_BRINGUP_LIMIT:-180}"
WATCHDOG_PID=""

watchdog_arm() {
    [ "$BRINGUP_LIMIT" -gt 0 ] 2>/dev/null || return 0
    _target=$$
    (
        _n=0
        while [ "$_n" -lt "$BRINGUP_LIMIT" ]; do
            kill -0 "$_target" 2>/dev/null || exit 0
            sleep 1
            _n=$((_n + 1))
        done
        echo "@@ [ad5x] bring-up exceeded ${BRINGUP_LIMIT}s - aborting so the" \
             "printer can finish booting. BOOT_FLAG_FAILURE stays set, so the" \
             "next boot runs stock." >&2
        kill -TERM "$_target" 2>/dev/null
        sleep 2
        kill -KILL "$_target" 2>/dev/null
    ) &
    WATCHDOG_PID=$!
}

watchdog_disarm() {
    [ -n "$WATCHDOG_PID" ] || return 0
    kill "$WATCHDOG_PID" 2>/dev/null
    WATCHDOG_PID=""
}

# ---------------------------------------------------------------------------
# network - take over what the stock UI owned
# ---------------------------------------------------------------------------
#
# Stock app_startup.sh takes eth0 DOWN and sets its MAC, and never brings it
# back up:
#
#     ifconfig eth0 down
#     ifconfig eth0 hw ether $MAC
#
# firmwareExe does the rest - `ifconfig eth0 up` and
# `udhcpc -i eth0 -p /var/run/udhcpc.pid` are both literals inside that binary,
# and the second is the exact cmdline of the live udhcpc on a stock boot. The Qt
# UI is the network manager on this board, for wlan0 as well as eth0.
#
# Step 2 kills it so HEADLESS can own the framebuffer, which silently takes the
# network with it. A completely successful bring-up then presents as a dead
# printer: no Moonraker, no ssh, and a frozen last frame on the panel, because
# every channel out needs the interface that just went away. That cost two days
# of chasing a boot "hang" that had already logged `Bootstrap complete`. ZMOD
# does not kill firmwareExe on the AD5X, which is why ZMOD keeps its network.
#
# So: whoever stops the stock UI has to take over what it owned.
NET_IFACES="${AD5X_NET_IFACES:-eth0}"
NET_WAIT="${AD5X_NET_WAIT:-20}"

iface_has_ipv4() {
    ifconfig "$1" 2>/dev/null | grep -q 'inet addr:' && return 0
    ip -4 addr show "$1" 2>/dev/null | grep -q 'inet ' && return 0
    return 1
}

## The stock UI also owns the buzzer, and plays a melody at startup. firmwareExe
## drives it through `cmd_pwm set_level pc12 100` - that literal sits among its
## buzzerPlay symbols, and pc12 is the only PWM channel it names outright (the
## backlight is a separate GPIO, backlight_gpio0). Killing the UI mid-note leaves
## the channel latched with nothing alive to call buzzerPlay::stop(), so the
## printer screams until it is power cycled; measured at level 1533857 on the rig,
## and it took a manual `cmd_pwm disable_channels pc12` to stop.
##
## ZMOD sidesteps this by bind-mounting /bin/echo over /usr/bin/cmd_pwm in its own
## app_startup.sh hook - a blanket mute for the whole boot. We only need to
## release the note we interrupted, so the buzzer still works afterwards.
BUZZER_CMD="${AD5X_BUZZER_CMD:-cmd_pwm}"
BUZZER_PWM="${AD5X_BUZZER_PWM:-pc12}"

silence_stock_buzzer() {
    if ! command -v "$BUZZER_CMD" >/dev/null 2>&1; then
        echo "// [ad5x] Buzzer: $BUZZER_CMD not available, skipping"
        return 0
    fi
    "$BUZZER_CMD" set_wc "$BUZZER_PWM" 0 0 >/dev/null 2>&1
    "$BUZZER_CMD" disable_channels "$BUZZER_PWM" >/dev/null 2>&1
    echo "// [ad5x] Buzzer: released $BUZZER_PWM"
    return 0
}

## Stopping the stock UI, silencing the note it was mid-way through, and taking
## over its network role are ONE operation, not three steps that happen to be
## adjacent. Every one of those jobs belonged to firmwareExe, and each time we
## took the UI without taking a job with it we got a failure that looked like
## something else entirely - an unreachable printer, then a screaming one. So
## they get a single name and a single test.
stop_stock_ui() {
    local proc

    echo "// [ad5x] Stopping stock UI ($STOCK_UI_PROCS)..."
    for proc in $STOCK_UI_PROCS; do
        killall "$proc" >/dev/null 2>&1 || true
    done

    silence_stock_buzzer

    echo "// [ad5x] Taking over the network from the stock UI..."
    provide_network
}

provide_network() {
    local n=0 iface

    for iface in $NET_IFACES; do
        if [ ! -d "/sys/class/net/$iface" ]; then
            echo "// [ad5x] Network: $iface not present, skipping"
            continue
        fi
        if iface_has_ipv4 "$iface"; then
            echo "// [ad5x] Network: $iface already addressed, leaving it alone"
            continue
        fi
        ifconfig "$iface" up 2>/dev/null
        # Same client and pid-file convention firmwareExe used, backgrounded so
        # the bring-up is never blocked by a missing DHCP server.
        udhcpc -i "$iface" -p "/var/run/udhcpc.$iface.pid" >/dev/null 2>&1 &
        echo "// [ad5x] Network: brought $iface up, udhcpc started (pid $!)"
    done

    [ "$NET_WAIT" -gt 0 ] 2>/dev/null || return 0
    while [ "$n" -lt "$NET_WAIT" ]; do
        for iface in $NET_IFACES; do
            if iface_has_ipv4 "$iface"; then
                echo "// [ad5x] Network up on $iface after ${n}s"
                return 0
            fi
        done
        sleep 1
        n=$((n + 1))
    done
    # Not fatal. A printer with no network is still a printer, and the bring-up
    # has more to do; but say so loudly, because everything downstream that
    # looks broken will actually be this.
    echo "@@ [ad5x] Network: no IPv4 on ($NET_IFACES) after ${NET_WAIT}s." \
         "Moonraker and ssh will be unreachable even if the bring-up completes." >&2
    return 0
}

# ---------------------------------------------------------------------------
# klippy launch - detached, then a bounded wait for its socket
# ---------------------------------------------------------------------------
#
# zstart_klipper.sh ends in `exec "$KLIPPER_DIR/start.sh"`, and on the AD5X that
# is the STOCK launcher: `/usr/prog/klipper/klipperDaemon start`. Nothing on this
# board treats that as self-daemonising. The stock Qt UI runs it as
# "/usr/prog/klipper/start.sh &" (the literal is in the firmwareExe binary), and
# ZMOD does not call it at all - it bind-mounts its own klipper13.sh over the
# path, which starts klippy through `start-stop-daemon -S -b` and returns.
#
# Calling it in the foreground therefore risks never returning, which takes
# Step 8 with it: no Moonraker, no dropbear, no network, and a printer
# recoverable only over USB. Launch it detached, like both references do.
KLIPPY_UDS="${AD5X_KLIPPY_UDS:-/tmp/uds}"
KLIPPY_WAIT="${AD5X_KLIPPY_WAIT:-30}"
KLIPPER_LAUNCH_PID=""

launch_klipper() {
    "$CMDS"/zstart_klipper.sh > /dev/null 2>&1 &
    KLIPPER_LAUNCH_PID=$!
}

# Bounded wait for klippy's unix socket, purely so the common case is logged as
# a fact rather than raced. Never fatal: Moonraker retries its klippy connection,
# so a timeout costs a slower first connect, not a failed boot.
wait_for_klippy_socket() {
    local n=0
    [ "$KLIPPY_WAIT" -gt 0 ] 2>/dev/null || return 0
    while [ "$n" -lt "$KLIPPY_WAIT" ]; do
        if [ -e "$KLIPPY_UDS" ]; then
            echo "// [ad5x] Klipper socket up after ${n}s ($KLIPPY_UDS)"
            return 0
        fi
        sleep 1
        n=$((n + 1))
    done
    # Whether the launcher itself is still running says which failure this is: a
    # launcher still alive with no socket is klipperDaemon not returning (the
    # thing this detach exists to survive); a launcher already gone means klippy
    # started and died, and its log is the place to look.
    if [ -n "$KLIPPER_LAUNCH_PID" ] && kill -0 "$KLIPPER_LAUNCH_PID" 2>/dev/null; then
        echo "@@ [ad5x] Klipper socket $KLIPPY_UDS absent after ${KLIPPY_WAIT}s and" \
             "the launcher (pid $KLIPPER_LAUNCH_PID) is still running - it is not" \
             "returning. Continuing; Moonraker retries its klippy connection." >&2
    else
        echo "@@ [ad5x] Klipper socket $KLIPPY_UDS absent after ${KLIPPY_WAIT}s and" \
             "the launcher already exited - check the klippy log. Continuing;" \
             "Moonraker retries its klippy connection." >&2
    fi
    return 0
}

failsafe_disarm() {
    rm -f "$BOOT_FAILURE_F"
}

# ---------------------------------------------------------------------------
# bring-up log - the only record a failed boot leaves behind
# ---------------------------------------------------------------------------
#
# Without this a boot failure is invisible. The hook runs inside stock
# app_startup.sh, whose stdout goes nowhere on this board, and every service
# that could otherwise carry a log - Moonraker, dropbear - starts in Step 8,
# after anything that goes wrong. That is why diagnosing the first hang needed a
# bespoke USB stick. ZMOD keeps the equivalent file at mod_data/log/zmod.log.
#
# The path is derived from $DATA_MNT, not from $MOD_DATA: MOD_DATA is a /opt
# path and /opt is not bound until Step 2, so it does not resolve yet at the
# point the log needs arming.
BRINGUP_LOG_KEEP="${AD5X_BRINGUP_LOG_KEEP:-3}"

bringup_log_start() {
    local dir="${AD5X_BRINGUP_LOG_DIR:-$DATA_MNT/config/mod_data/log}"
    local log="$dir/ad5x_bootstrap.log"
    local i

    # Never fatal: a bring-up that cannot open a log must still boot the
    # printer. `touch`, never `: >` - a redirection failure on the `:` special
    # builtin exits the shell outright.
    mkdir -p "$dir" 2>/dev/null || return 0
    touch "$log" 2>/dev/null || return 0

    # KEEP counts the live log too, so the highest suffix is KEEP-1: KEEP=3
    # leaves ad5x_bootstrap.log plus .1 and .2. Drop anything above that, which
    # also cleans up after a larger KEEP on an earlier boot.
    [ "$BRINGUP_LOG_KEEP" -ge 1 ] 2>/dev/null || BRINGUP_LOG_KEEP=1
    rm -f "$log.$BRINGUP_LOG_KEEP" 2>/dev/null
    i=$((BRINGUP_LOG_KEEP - 1))
    while [ "$i" -gt 1 ]; do
        mv -f "$log.$((i - 1))" "$log.$i" 2>/dev/null
        i=$((i - 1))
    done
    [ "$BRINGUP_LOG_KEEP" -gt 1 ] && mv -f "$log" "$log.1" 2>/dev/null

    # Truncate, not append: the previous boot's record was just rotated away,
    # and with KEEP=1 there is no rotation, so appending would grow forever.
    exec >"$log" 2>&1
    echo "=== [ad5x] bring-up start $(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null) pid $$ ==="
    return 0
}

run_bootstrap() {
    local dry_run=0
    case "${1:-}" in
        --dry-run) dry_run=1 ;;
        "")        : ;;
        *)         echo "ad5x_bootstrap: run: unknown option '$1'" >&2; return 2 ;;
    esac

    # Step 1 (guard half): the platform descriptor decides whether we run at all.
    # shellcheck disable=SC1090,SC1091
    . "$SELF_DIR/platform.sh"

    if [ "${PLATFORM:-}" != "ad5x" ]; then
        echo "ad5x_bootstrap: refusing to run on PLATFORM='${PLATFORM:-unknown}' (this bootstrap is AD5X-only)" >&2
        return 1
    fi

    # Real runs log to a file from here on; the dry-run must keep its stdout so
    # callers (and the tests) can read the plan.
    [ "$dry_run" -eq 1 ] || bringup_log_start

    if [ "$dry_run" -eq 1 ]; then
        echo "[dry-run] AD5X headless bootstrap plan (PLATFORM=$PLATFORM):"
        echo "[dry-run] failsafe: stand down this boot if BOOT_FLAG_FAILURE/SKIP is set; otherwise arm BOOT_FLAG_FAILURE before step 2 and clear it after step 8"
        echo "[dry-run] 1. source platform.sh, common.sh, klipper_overlay.sh, init_lib.sh"
        echo "[dry-run] 2. preconditions: bind /opt->$DATA_MNT; provide /bin/bash (faithful /bin superset bound over /bin, no mod script modified); move aside \$MOD/ZMOD marker; ensure variables.cfg display=HEADLESS, use_swap=OFF, show_feather_promo=0; stop stock UI ($STOCK_UI_PROCS), then take over its jobs: release the buzzer ($BUZZER_PWM) and the network (ifconfig up + udhcpc on $NET_IFACES, wait up to ${NET_WAIT}s)"
        echo "[dry-run] 3. mount_data_partition"
        echo "[dry-run] 4. init_buildroot"
        echo "[dry-run] 5. apply_klipper_patches"
        echo "[dry-run] 6. fix_config"
        echo "[dry-run] 7. launch klipper DETACHED (zstart_klipper.sh against the patched /usr/prog/klipper), then wait up to ${KLIPPY_WAIT}s for $KLIPPY_UDS"
        echo "[dry-run] 8. first-run database (migrate_db + restore_ota) then chroot start.sh (ntpd, Moonraker :7125, httpd :80)"
        echo "[dry-run] 9. done; stock app_startup.sh continues (it does not launch klippy)"
        echo "[dry-run] log: a real run tees steps 1-9 to \$DATA_MNT/config/mod_data/log/ad5x_bootstrap.log (keep ${BRINGUP_LOG_KEEP})"
        return 0
    fi

    # Step 1 (rest): the heavier libraries, only in a real run.
    # shellcheck disable=SC1090,SC1091
    . "$SELF_DIR/common.sh"
    # shellcheck disable=SC1090,SC1091
    . "$SELF_DIR/klipper_overlay.sh"
    # shellcheck disable=SC1090,SC1091
    . "$SELF_DIR/init_lib.sh"

    # Failsafe gate: a prior boot that never finished, or an explicit skip
    # request, means stand down this boot and let stock come up clean. Otherwise
    # arm the failure flag - only a completed bring-up (below) clears it.
    if failsafe_should_skip; then
        # Standing the mod down is not enough on its own. apply_klipper_patches
        # overlays STOCK klippy (KLIPPER_DIR is /usr/prog/klipper on this board),
        # and those symlinks outlive the mod that made them: stock then imports
        # patched modules whose runtime environment is gone, fails to start, and
        # the printer sits on "Initializing..." with the board alarming. A
        # half-finished bring-up leaves exactly that state behind, so undo it
        # here - that is what turns a failed bring-up into a clean stock boot
        # instead of a machine recoverable only over USB.
        echo "// [ad5x] Reverting the klipper overlay so stock can boot clean..."
        revert_klipper_patches || \
            echo "@@ [ad5x] Overlay revert reported an error; stock klippy may not start." >&2
        return 0
    fi
    failsafe_arm
    watchdog_arm

    # Step 2. Preconditions no installer set up on the AD5X.
    echo "// [ad5x] Establishing preconditions..."
    # The boot scripts carry ~140 /opt/config/mod literals by design; the /opt
    # bind resolves them to $MOD_ROOT. init_buildroot binds $DATA_MNT into the
    # chroot itself (as $MOD/data), so no host /data alias is needed - and the
    # AD5X host / is a read-only squashfs on which a /data mountpoint cannot be
    # created anyway.
    _ensure_bind "$DATA_MNT" /opt
    # The AD5X host squashfs ships no /bin/bash and the kernel has no overlayfs,
    # so klippy's #!/bin/bash gcode_shell_command targets cannot start. Provide
    # /bin/bash by binding a faithful /bin superset (provide_host_bash); no mod
    # script is modified. Must precede the klippy launch (Step 7).
    provide_host_bash
    # A $MOD/ZMOD marker triggers a 30s zversion stall plus ARM tone.py. Move it
    # aside (reversible) rather than deleting it.
    if [ -e "$MOD/ZMOD" ] && [ ! -e "$MOD/ZMOD.forge-x-disabled" ]; then
        mv "$MOD/ZMOD" "$MOD/ZMOD.forge-x-disabled"
    fi
    # Headless, and no swap (the zram .ko are the AD5M ARM kernel's).
    if [ ! -f "$VAR_PATH" ]; then
        mkdir -p "$(dirname "$VAR_PATH")"
        : > "$VAR_PATH"
    fi
    _ensure_var display HEADLESS
    _ensure_var use_swap OFF
    # AD5X has no e0_sensor filament switch (its stock config defines none); the
    # runout guards read this variable, so default it off. Filament presence on
    # the AD5X comes from the IFS, wired up separately in the print flow.
    _ensure_var filament_switch_sensor 0
    # The AD5X runs headless with no working Feather on-device UI, so the one-shot
    # "Try Feather" promo (config/display_offer.cfg, which pops an action:prompt in
    # the web UI) is only noise here. Default it off; a user can still re-enable it.
    _ensure_var show_feather_promo 0

    # Stock app_startup.sh launches the Qt UI (firmwareExe) before our hook runs,
    # and that UI is also this board's network manager. stop_stock_ui() does both
    # halves - see its comment for why they are inseparable.
    stop_stock_ui

    # Step 3. No-op on AD5X (/usr/data is already mounted); parity with S00init.
    echo "// [ad5x] Mounting data partition..."
    mount_data_partition

    # Step 4. chroot mounts + binds + printer_data compat tree + www + zhttp apply.
    echo "// [ad5x] Buildroot initialization..."
    init_buildroot

    # Step 5. Overlay Forge-X's klipper extras/patches BEFORE stock launches klippy.
    echo "// [ad5x] Applying klipper patches..."
    if ! apply_klipper_patches; then
        echo "@@ [ad5x] Failed to apply klipper patches." >&2
        watchdog_disarm
        return 1
    fi

    # Step 6. Write printer.cfg (display resolves to init.display.headless.cfg).
    echo "// [ad5x] Restoring config..."
    fix_config

    # Step 7. Launch klippy, detached. Stock app_startup.sh never launches it
    # (unlike the AD5M, where S55boot/boot.sh runs zstart_klipper), and Step 2
    # stopped the Qt UI that otherwise would, so the mod owns the launch - the
    # same MIPS-safe launcher, run against the just-patched /usr/prog/klipper
    # through the host Python env in its start.sh. See launch_klipper() for why
    # it must not be called in the foreground.
    echo "// [ad5x] Launching klipper..."
    launch_klipper
    wait_for_klippy_socket

    # Step 8. First-run DB seed, then bring up the chroot services. The chroot
    # target paths are resolved INSIDE $MOD, where init_buildroot bind-mounts
    # /opt/config; they are the exact paths S99root uses, not host literals.
    if [ ! -f "$MOD/root/printer_data/database/moonraker-sql.db" ]; then
        echo "// [ad5x] First run: seeding database..."
        "$SCRIPTS"/migrate_db.sh
        chroot "$MOD" /opt/config/mod/.root/restore_ota.sh
    fi
    echo "// [ad5x] Starting Buildroot services..."
    chroot "$MOD" /opt/config/mod/.root/start.sh

    # Bring-up completed: stand the watchdog down, then clear the failure flag
    # so the next boot re-arms the mod.
    watchdog_disarm
    failsafe_disarm

    # Step 9. Done: klippy (Step 7) and the chroot services (Step 8) are up. Stock
    # app_startup.sh continues; it does not launch klippy itself.
    echo "// [ad5x] Bootstrap complete."
    return 0
}

# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

main() {
    case "${1:-}" in
        install)      shift; install_hook "$@" ;;
        uninstall)    shift; uninstall_hook "$@" ;;
        run)          shift; run_bootstrap "$@" ;;
        ""|--dry-run) run_bootstrap "$@" ;;
        *)
            echo "Usage: ${0##*/} {install|uninstall|run [--dry-run]}" >&2
            return 1
        ;;
    esac
}

# Auto-run only when executed, not when sourced: the test suite sources this
# file to call run_bootstrap in-process with a shadowed uname.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
    exit $?
fi
