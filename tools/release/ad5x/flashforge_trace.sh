#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
#
## Forge-X AD5X TRACING stick.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license.
##
## Runs the AD5X bring-up under a full shell trace with a hard timeout, so the
## LAST LINE of the trace names the command that wedges the boot. Installs
## nothing permanent and never reboots.
##
## Why over USB rather than SSH: only when the printer will not boot far enough
## to bring up networking. Stock AD5X firmware DOES run dropbear - /usr/sbin/dropbear
## is in the read-only squashfs, started by /etc/init.d/S50dropbear - so a printer
## that boots stock at all is usually reachable at root@<ip> with the mod fully
## disarmed. (.shell/S60dropbear is the mod's CHROOT dropbear, a different thing;
## mistaking it for the only one cost a session of building sticks for questions
## one ssh answers.) Try the network first; reach for this when that fails.
##
## Order:
##   0. RECOVER  - lift any earlier forgex-debug reports off the data partition
##                 (the updater extracts to /usr/data/update, which persists).
##   1. TRACE    - run the bring-up under `bash -x` with a timeout.
##   2. SAVE     - trace + supporting state to the stick.
##   3. DISARM   - leave the bootstrap non-executable so the printer still
##                 boots stock afterwards.
##   4. SIGNAL   - solid white = trace captured, dark grey = could not run it.
##
## No `set -e`: a failed probe must never abort the run.

FF_DATA_MNT="${FF_DATA_MNT:-/usr/data}"
FF_FB="${FF_FB:-/dev/fb0}"
TRACE_LIMIT="${FF_TRACE_LIMIT:-90}"

WORK_DIR=$(dirname "$0")
MOD_ROOT_DIR="$FF_DATA_MNT/config/mod"
MOD_DIR="$FF_DATA_MNT/.mod/.forge-x"
BOOTSTRAP_SH="$MOD_ROOT_DIR/.shell/ad5x_bootstrap.sh"
# The RTC is not set this early: `date` returns 19700101-000005 on every single
# run, so a timestamp alone silently overwrites the previous run's report. $$ is
# the one cheap source of per-run uniqueness available here.
STAMP="$(date '+%Y%m%d-%H%M%S' 2>/dev/null || echo unknown)-$$"

OUT_DIRS=""
_add_out() {
    _d="$1"
    [ -n "$_d" ] || return 0
    mkdir -p "$_d" 2>/dev/null || return 0
    # `touch`, never `: >` - a redirection failure on the `:` special builtin
    # is fatal to a POSIX shell and would kill this script silently.
    if touch "$_d/.wtest" 2>/dev/null; then
        rm -f "$_d/.wtest" 2>/dev/null
        OUT_DIRS="$OUT_DIRS $_d"
    fi
}
_add_out "$WORK_DIR/forgex-trace"
_add_out "$FF_DATA_MNT/forgex-trace"
for _mp in $(mount 2>/dev/null | awk '/vfat|msdos|fuseblk/ {print $3}'); do
    _add_out "$_mp/forgex-trace"
done

REPORT=""
for _d in $OUT_DIRS; do REPORT="$_d/trace-report-$STAMP.txt"; break; done
[ -n "$REPORT" ] || REPORT=/tmp/trace-report-$STAMP.txt

say() {
    echo "$@"
    echo "$@" >> "$REPORT" 2>/dev/null
}

distribute() {
    for _f in "$@"; do
        [ -f "$_f" ] || continue
        for _d in $OUT_DIRS; do
            [ "$_d/$(basename "$_f")" = "$_f" ] && continue
            cp "$_f" "$_d/" 2>/dev/null
        done
    done
}

fb_fill() {
    [ -w "$FF_FB" ] || return 0
    _stride=$(cat /sys/class/graphics/fb0/stride 2>/dev/null)
    _vs=$(cat /sys/class/graphics/fb0/virtual_size 2>/dev/null)
    _h=$(echo "$_vs" | cut -d, -f2)
    case "$_stride" in ''|*[!0-9]*) _stride=3200 ;; esac
    case "$_h" in ''|*[!0-9]*) _h=960 ;; esac
    dd if=/dev/zero bs="$_stride" count="$_h" 2>/dev/null | tr '\000' "$1" > "$FF_FB" 2>/dev/null
}

say "[ad5x-trace] Forge-X AD5X tracing stick  $STAMP  arch=$(uname -m 2>/dev/null)"
say "[ad5x-trace] sinks:$OUT_DIRS"
say "[ad5x-trace] INSTALLS NOTHING PERMANENT, NEVER REBOOTS."

# --------------------------------------------------------------------------
# 0. RECOVER earlier reports. The stock updater extracts into /usr/data/update,
#    which is a real ext4 partition, so a previous run's report is still there
#    even though it never reached the stick.
# --------------------------------------------------------------------------
say ""
say "[ad5x-trace] 0. RECOVER earlier reports"
_found=0
for _d in "$FF_DATA_MNT/update/forgex-debug" "$FF_DATA_MNT/forgex-debug" \
          "$FF_DATA_MNT/update/forgex-trace" "$FF_DATA_MNT/forgex-trace"; do
    [ -d "$_d" ] || continue
    for _f in "$_d"/*; do
        [ -f "$_f" ] || continue
        case "$(basename "$_f")" in recovered-*) continue ;; esac
        for _o in $OUT_DIRS; do
            cp "$_f" "$_o/recovered-$(basename "$_d")-$(basename "$_f")" 2>/dev/null
        done
        _found=$((_found + 1))
    done
done
say "  recovered $_found earlier file(s)"

# --------------------------------------------------------------------------
# 1. TRACE the bring-up.
#
# The bootstrap's first act is a trampoline that re-execs itself under the
# chroot's bash. `exec` replaces the process and does NOT carry -x across, so
# tracing the outer sh would go blind at exactly the wrong moment. Instead,
# invoke the chroot bash directly with -x and set _FORGEX_BASHED so the
# trampoline sees itself as already done and falls through.
# --------------------------------------------------------------------------
say ""
say "[ad5x-trace] 1. TRACE (limit ${TRACE_LIMIT}s)"

# Clear the failsafe flags first. BOOT_FLAG_SKIP is set by the rescue stick, and
# failsafe_should_skip() consumes it and returns before any bring-up work
# happens - which made the previous traced run a 1-second no-op. The bootstrap
# re-arms BOOT_FLAG_FAILURE itself; the DISARM step below cleans up after.
for _f in "$MOD_ROOT_DIR/BOOT_FLAG_SKIP" "$MOD_ROOT_DIR/BOOT_FLAG_FAILURE"; do
    if [ -f "$_f" ]; then
        rm -f "$_f" 2>/dev/null && say "  cleared $(basename "$_f") so the bring-up really runs"
    fi
done
TRACE_OUT="$(dirname "$REPORT")/bringup-trace-$STAMP.log"
LD="$MOD_DIR/lib/ld-linux-mipsn8.so.1"
BASH_BIN="$MOD_DIR/bin/bash"
TRACED_OK=no

if [ ! -f "$BOOTSTRAP_SH" ]; then
    say "  !! bootstrap missing: $BOOTSTRAP_SH"
elif [ ! -x "$LD" ] || [ ! -f "$BASH_BIN" ]; then
    say "  !! chroot bash/loader missing ($LD / $BASH_BIN); falling back to sh -x"
    _FORGEX_BASHED="$BOOTSTRAP_SH" sh -x "$BOOTSTRAP_SH" run > "$TRACE_OUT" 2>&1 &
    _pid=$!
    TRACED_OK=yes
else
    say "  running: bash -x $BOOTSTRAP_SH run   (trampoline skipped)"
    _FORGEX_BASHED="$BOOTSTRAP_SH" \
        "$LD" --library-path "$MOD_DIR/lib:$MOD_DIR/usr/lib" "$BASH_BIN" \
        -x "$BOOTSTRAP_SH" run > "$TRACE_OUT" 2>&1 &
    _pid=$!
    TRACED_OK=yes
fi

if [ "$TRACED_OK" = yes ]; then
    _n=0
    while [ "$_n" -lt "$TRACE_LIMIT" ]; do
        kill -0 "$_pid" 2>/dev/null || break
        sleep 1
        _n=$((_n + 1))
    done
    if kill -0 "$_pid" 2>/dev/null; then
        say "  TIMED OUT after ${TRACE_LIMIT}s - this is the hang, killing it."
        echo "" >> "$TRACE_OUT"
        echo "########## TIMED OUT AFTER ${TRACE_LIMIT}s - THE LINE ABOVE IS WHERE IT HANGS ##########" >> "$TRACE_OUT"
        kill -9 "$_pid" 2>/dev/null
        # Anything the bring-up spawned that is still holding the boot.
        { echo ""; echo "===== processes at timeout ====="; ps; } >> "$TRACE_OUT" 2>&1
    else
        say "  bring-up finished on its own in ${_n}s (did NOT hang this time)"
        echo "" >> "$TRACE_OUT"
        echo "########## COMPLETED WITHOUT HANGING (${_n}s) ##########" >> "$TRACE_OUT"
    fi
    say "  trace lines: $(wc -l < "$TRACE_OUT" 2>/dev/null)"
    say "  last command traced:"
    say "    $(grep '^+' "$TRACE_OUT" 2>/dev/null | tail -1)"
fi

# --------------------------------------------------------------------------
# 2. SAVE supporting state.
# --------------------------------------------------------------------------
say ""
say "[ad5x-trace] 2. SAVE"
{
    echo "===== mounts after the traced run ====="; mount
    echo ""; echo "===== processes ====="; ps
    echo ""; echo "===== dmesg tail ====="; dmesg 2>/dev/null | tail -200
    echo ""; echo "===== klippy/moonraker logs ====="
    # $FF_DATA_MNT/logs is the stock klippy log dir on this board (stock
    # app_startup.sh opens by removing /usr/data/logs/printer.log*); the
    # printer_data trees are the mod compat layout.
    for l in "$FF_DATA_MNT"/logs/*.log "$FF_DATA_MNT"/printer_data/logs/*.log \
             "$MOD_DIR"/root/printer_data/logs/*.log; do
        [ -f "$l" ] && { echo "--- $l ---"; tail -80 "$l"; }
    done
    # Did Step 6 (fix_config) actually complete? The AD5X keeps its config in
    # $FF_DATA_MNT/config - the factory installer writes it there, ZMOD launches
    # klippy against /usr/data/config/printer.cfg, and the mod's fix_config
    # writes /opt/config/printer.cfg, the same dir once /opt is bound.
    echo ""; echo "===== config after the traced run ====="
    for c in "$FF_DATA_MNT"/config/printer.cfg "$FF_DATA_MNT"/config/printer.base.cfg; do
        if [ -f "$c" ]; then
            echo "--- $c ($(wc -c < "$c") bytes, mtime $(date -r "$c" 2>/dev/null)) ---"
            head -60 "$c"
        else
            echo "--- $c: ABSENT ---"
        fi
    done
} >> "$REPORT" 2>&1

for _c in "$FF_DATA_MNT"/config/printer.cfg "$FF_DATA_MNT"/config/printer.base.cfg; do
    [ -f "$_c" ] || continue
    for _d in $OUT_DIRS; do
        cp "$_c" "$_d/cfg-$(basename "$_c")" 2>/dev/null
    done
done

distribute "$TRACE_OUT" "$REPORT"

# --------------------------------------------------------------------------
# 3. DISARM again - the traced run may have re-enabled things, and the printer
#    must still come up stock after this stick is pulled.
# --------------------------------------------------------------------------
say ""
say "[ad5x-trace] 3. DISARM"
chmod a-x "$BOOTSTRAP_SH" 2>/dev/null
if [ -x "$BOOTSTRAP_SH" ]; then
    say "  !! bootstrap is STILL executable - it may run on next boot"
else
    say "  bootstrap left non-executable; next boot is stock"
fi
mkdir -p "$MOD_ROOT_DIR" 2>/dev/null && touch "$MOD_ROOT_DIR/BOOT_FLAG_SKIP" 2>/dev/null
rm -f "$MOD_ROOT_DIR/BOOT_FLAG_FAILURE" 2>/dev/null

# Remove ourselves from the stick so a power cycle with it still inserted does
# not re-run the whole thing (the stock updater re-reads the stick every boot).
for _mp in $(mount 2>/dev/null | awk '/vfat|msdos|fuseblk/ {print $3}') /mnt; do
    [ -d "$_mp" ] || continue
    for _img in "$_mp"/AD5X-*.tgz "$_mp"/AD5X-*.tar; do
        [ -f "$_img" ] && rm -f "$_img" 2>/dev/null && say "  removed $_img"
    done
done

distribute "$TRACE_OUT" "$REPORT"
sync

say ""
if [ "$TRACED_OK" = yes ]; then
    say "[ad5x-trace] DONE - trace captured. SOLID WHITE. Pull stick, power cycle."
    fb_fill '\377'
else
    say "[ad5x-trace] DONE - could NOT run the bring-up. SOLID DARK GREY."
    fb_fill '\044'
fi
distribute "$TRACE_OUT" "$REPORT"
sync

exit 0
