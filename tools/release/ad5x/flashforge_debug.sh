#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
#
## Forge-X AD5X RESCUE + DIAGNOSTIC stick.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license.
##
## Drop-in replacement for flashforge_init.sh that INSTALLS NOTHING and NEVER
## REBOOTS. The stock updater runs it exactly the same way (argv "MACHINE PID").
##
## It exists because the AD5X wedged twice at "initialization": the mod's boot
## hook runs synchronously inside stock app_startup.sh, so anything that hangs
## in there stops the whole boot, and the installer's post-install sysrq reboot
## re-triggered the updater off the still-inserted stick - an install loop.
##
## What it does, in order:
##   1. RESCUE   - unwire the boot hook so the printer comes up pure stock.
##   2. COLLECT  - dump everything needed to explain the hang.
##   3. MEASURE  - report the real framebuffer geometry (this is also what the
##                 corrupted status frames need in order to be generated right).
##   4. ARM      - leave a traced, timeout-guarded runner on the device so the
##                 bring-up can be reproduced over SSH once stock is up.
##   5. SIGNAL   - fill the screen solid white and stop. Solid fills are immune
##                 to the stride/channel-order bugs that corrupt our frames.
##
## Deliberately NO `set -e`: a failed probe must never abort the collection.

FF_DATA_MNT="${FF_DATA_MNT:-/usr/data}"
FF_FB="${FF_FB:-/dev/fb0}"

WORK_DIR=$(dirname "$0")
MACHINE=$1
PID=$2

MOD_ROOT_DIR="$FF_DATA_MNT/config/mod"
MOD_DIR="$FF_DATA_MNT/.mod/.forge-x"
APP_STARTUP="${AD5X_APP_STARTUP:-/usr/prog/app_startup.sh}"
HOOK_MARKER='# forge-x'

STAMP=$(date '+%Y%m%d-%H%M%S' 2>/dev/null || echo unknown)

# --------------------------------------------------------------------------
# Output sinks. The stick is what Preston can physically read, but WORK_DIR is
# wherever the stock updater chose to extract us and may be volatile, so mirror
# everything to the data partition too and to any writable vfat we can find.
# --------------------------------------------------------------------------
OUT_DIRS=""

_add_out() {
    _d="$1"
    [ -n "$_d" ] || return 0
    mkdir -p "$_d" 2>/dev/null || return 0
    # Prove it is actually writable before we count on it.
    if touch "$_d/.forgex-write-test" 2>/dev/null; then
        rm -f "$_d/.forgex-write-test" 2>/dev/null
        OUT_DIRS="$OUT_DIRS $_d"
    fi
}

_add_out "$WORK_DIR/forgex-debug"
_add_out "$FF_DATA_MNT/forgex-debug"
# Any mounted vfat/msdos filesystem is almost certainly the stick itself.
for _mp in $(mount 2>/dev/null | awk '/vfat|msdos|fuseblk/ {print $3}'); do
    _add_out "$_mp/forgex-debug"
done

REPORT=""
for _d in $OUT_DIRS; do
    REPORT="$_d/forgex-diag-$STAMP.txt"
    break
done
[ -n "$REPORT" ] || REPORT=/tmp/forgex-diag-$STAMP.txt
mkdir -p "$(dirname "$REPORT")" 2>/dev/null

say() {
    echo "$@"
    echo "$@" >> "$REPORT" 2>/dev/null
}

# Copy the report into EVERY output dir. The v2 run lost its whole report
# because $REPORT lived in the updater's extract dir, which is volatile, while
# only grab()'d files went to all sinks. Called at each checkpoint, not just at
# the end, so an early abort still leaves us something.
distribute_report() {
    [ -f "$REPORT" ] || return 0
    for _d in $OUT_DIRS; do
        [ "$_d/$(basename "$REPORT")" = "$REPORT" ] && continue
        cp "$REPORT" "$_d/" 2>/dev/null
    done
}

# Run a probe and capture it under a labelled banner. Never fails the script.
probe() {
    _label="$1"; shift
    {
        echo ""
        echo "===== $_label ====="
        "$@" 2>&1
        echo "--- (exit $?) ---"
    } >> "$REPORT" 2>/dev/null
}

# Copy a file into every output dir, flattened and prefixed.
grab() {
    _src="$1"; _as="$2"
    [ -f "$_src" ] || { say "  (absent) $_src"; return 0; }
    for _d in $OUT_DIRS; do
        cp "$_src" "$_d/$_as" 2>/dev/null
    done
    say "  captured $_src -> $_as"
}

# --------------------------------------------------------------------------
# Solid-colour framebuffer fill. Every pixel gets the same byte, so the result
# is a clean uniform screen no matter the bpp, channel order, or stride - the
# exact properties our xz raw frames get wrong. Geometry is read from sysfs
# rather than assumed.
# --------------------------------------------------------------------------
fb_fill() {
    _byte="$1"
    [ -w "$FF_FB" ] || return 0
    _stride=$(cat /sys/class/graphics/fb0/stride 2>/dev/null)
    _vsize=$(cat /sys/class/graphics/fb0/virtual_size 2>/dev/null)
    _bpp=$(cat /sys/class/graphics/fb0/bits_per_pixel 2>/dev/null)
    _h=$(echo "$_vsize" | cut -d, -f2)
    _w=$(echo "$_vsize" | cut -d, -f1)
    # Fall back to the size the installer always assumed, then to a generous
    # over-write (a short write at the end of fb0 is harmless).
    case "$_stride" in ''|*[!0-9]*) _stride="" ;; esac
    if [ -z "$_stride" ] && [ -n "$_w" ] && [ -n "$_bpp" ]; then
        _stride=$(( _w * _bpp / 8 ))
    fi
    [ -n "$_stride" ] || _stride=1920
    case "$_h" in ''|*[!0-9]*) _h=800 ;; esac
    dd if=/dev/zero bs="$_stride" count="$_h" 2>/dev/null \
        | tr '\000' "$_byte" > "$FF_FB" 2>/dev/null
}

# ==========================================================================
say "[ad5x-debug] Forge-X AD5X rescue + diagnostic stick"
say "[ad5x-debug] $STAMP  MACHINE='$MACHINE' PID='$PID' arch=$(uname -m 2>/dev/null)"
say "[ad5x-debug] report: $REPORT"
say "[ad5x-debug] mirroring to:$OUT_DIRS"
say "[ad5x-debug] THIS STICK INSTALLS NOTHING AND WILL NOT REBOOT."

# --------------------------------------------------------------------------
# 1. RESCUE - unwire the boot hook.
#
# BOOT_FLAG_SKIP alone is not enough: the flag is only consulted after the bash
# trampoline and four library sources, so a hang before that point never reads
# it and the printer wedges on every boot. Removing the hook line is the only
# recovery that cannot itself hang. Both are done; the flag is belt and braces.
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 0. SNAPSHOT the boot flags BEFORE the rescue step clears them.
#
# This is the most informative single bit we have about the wedge:
#   FAILURE present -> the bootstrap reached failsafe_arm and then hung
#                      somewhere in steps 2-8 (preconditions .. start.sh).
#   FAILURE absent  -> it never got that far, i.e. it died or hung in the bash
#                      trampoline or while sourcing platform/common/klipper_
#                      overlay/init_lib. The trampoline execs the CHROOT's
#                      ld.so + bash, so a missing or wrong-arch binary there
#                      hangs before a single line is ever logged.
# --------------------------------------------------------------------------
if [ -f "$MOD_ROOT_DIR/BOOT_FLAG_FAILURE" ]; then
    FLAG_FAILURE_AT_BOOT=present
else
    FLAG_FAILURE_AT_BOOT=absent
fi
if [ -f "$MOD_ROOT_DIR/BOOT_FLAG_SKIP" ]; then
    FLAG_SKIP_AT_BOOT=present
else
    FLAG_SKIP_AT_BOOT=absent
fi
say ""
say "[ad5x-debug] 0. BOOT FLAG SNAPSHOT (as found, before rescue)"
say "  BOOT_FLAG_FAILURE = $FLAG_FAILURE_AT_BOOT"
say "  BOOT_FLAG_SKIP    = $FLAG_SKIP_AT_BOOT"
if [ "$FLAG_FAILURE_AT_BOOT" = present ]; then
    say "  => bootstrap armed the failsafe, then hung in steps 2-8."
else
    say "  => bootstrap never reached failsafe_arm: suspect the bash"
    say "     trampoline (chroot ld.so/bash) or the library sourcing."
fi

say ""
say "[ad5x-debug] 1. RESCUE"
grab "$APP_STARTUP" "app_startup.sh.as-found"
grab "$APP_STARTUP.orig" "app_startup.sh.orig"

# The hook line stock app_startup.sh carries is:
#     [ -x <bootstrap> ] && <bootstrap>   # forge-x
# It is the LAST line of the file and the file has no `set -e`, so making the
# bootstrap non-executable turns the hook into a harmless no-op. This is the
# PRIMARY rescue because it only needs $MOD_ROOT on the writable data
# partition - the v2 run proved /usr/prog is read-only by the time we run,
# even though the installer wrote to it earlier.
BOOTSTRAP_SH="$MOD_ROOT_DIR/.shell/ad5x_bootstrap.sh"
RESCUE_OK=no
if [ -e "$BOOTSTRAP_SH" ]; then
    chmod a-x "$BOOTSTRAP_SH" 2>/dev/null
    if [ -x "$BOOTSTRAP_SH" ]; then
        say "  !! could not clear +x on $BOOTSTRAP_SH"
    else
        say "  cleared +x on $BOOTSTRAP_SH -> hook line is now a no-op"
        RESCUE_OK=yes
    fi
else
    say "  (absent) $BOOTSTRAP_SH - nothing for the hook to run anyway"
    RESCUE_OK=yes
fi

# Secondary: also try to unwire the line itself. /usr/prog is usually read-only
# at this point, so attempt a remount first and treat failure as non-fatal -
# the chmod above is what actually saves the boot.
mount -o remount,rw /usr/prog 2>/dev/null && say "  remounted /usr/prog rw"
if [ ! -f "$APP_STARTUP" ]; then
    say "  !! $APP_STARTUP not found - cannot unwire (is /usr/prog mounted?)"
elif ! grep -Fq -- "$HOOK_MARKER" "$APP_STARTUP" 2>/dev/null; then
    say "  hook not present; stock startup is already clean"
elif [ -e "$APP_STARTUP.orig" ]; then
    if cat "$APP_STARTUP.orig" > "$APP_STARTUP" 2>/dev/null; then
        rm -f "$APP_STARTUP.orig" 2>/dev/null
        say "  restored $APP_STARTUP from .orig  -> next boot is pure stock"
    else
        say "  !! restore FAILED (read-only /usr/prog?)"
    fi
else
    _t=$(mktemp 2>/dev/null || echo /tmp/fx.$$)
    if grep -Fv -- "$HOOK_MARKER" "$APP_STARTUP" > "$_t" 2>/dev/null \
       && [ -s "$_t" ] && cat "$_t" > "$APP_STARTUP" 2>/dev/null; then
        say "  stripped the '$HOOK_MARKER' line -> next boot is pure stock"
    else
        say "  !! strip FAILED (read-only /usr/prog?)"
    fi
    rm -f "$_t" 2>/dev/null
fi

# Trust the filesystem, not the exit status of whatever wrote to it. The boot
# is safe if EITHER the hook line is gone OR the bootstrap is non-executable.
_line_gone=no
if [ ! -f "$APP_STARTUP" ] || ! grep -Fq -- "$HOOK_MARKER" "$APP_STARTUP" 2>/dev/null; then
    _line_gone=yes
fi
if [ "$_line_gone" = no ] && [ -x "$BOOTSTRAP_SH" ]; then
    RESCUE_OK=no
    say "  !! VERIFY FAILED: hook line present AND bootstrap still executable"
elif [ "$_line_gone" = yes ]; then
    RESCUE_OK=yes
fi
say "  hook line removed : $_line_gone"
say "  bootstrap runnable: $( [ -x "$BOOTSTRAP_SH" ] && echo yes || echo no )"
say "  rescue verified   : $RESCUE_OK"

if mkdir -p "$MOD_ROOT_DIR" 2>/dev/null && touch "$MOD_ROOT_DIR/BOOT_FLAG_SKIP" 2>/dev/null; then
    say "  wrote BOOT_FLAG_SKIP (second line of defence)"
fi
rm -f "$MOD_ROOT_DIR/BOOT_FLAG_FAILURE" 2>/dev/null
sync

# --------------------------------------------------------------------------
# 2. COLLECT
# --------------------------------------------------------------------------
say ""
say "[ad5x-debug] 2. COLLECT"

# How far did the wedged boot actually get? These two flags are the single most
# informative bit we have: FAILURE present means the bootstrap armed it and then
# hung SOMEWHERE IN STEPS 2-8. Absent means it never even reached failsafe_arm,
# i.e. it died in the bash trampoline or while sourcing the four libraries.
{
    echo "===== boot flag state (as found, before rescue cleared them) ====="
    echo "NOTE: read before the rescue step removed BOOT_FLAG_FAILURE."
} >> "$REPORT" 2>/dev/null
probe "flags" ls -la "$MOD_ROOT_DIR/BOOT_FLAG_FAILURE" "$MOD_ROOT_DIR/BOOT_FLAG_SKIP"

probe "MOUNT TABLE (why is /usr/prog ro?)" sh -c 'mount; echo "---- /proc/mounts ----"; cat /proc/mounts'
probe "usr/prog writability" sh -c 'touch /usr/prog/.forgex-wtest 2>&1 && echo WRITABLE && rm -f /usr/prog/.forgex-wtest || echo READ-ONLY'
probe "uname -a"            uname -a
probe "cpuinfo"             cat /proc/cpuinfo
probe "mounts"              cat /proc/mounts
probe "df -h"               df -h
probe "processes"           ps
probe "dmesg (tail 300)"    sh -c 'dmesg 2>/dev/null | tail -300'
probe "free"                free
probe "fb0 sysfs"           sh -c 'for f in /sys/class/graphics/fb0/*; do [ -r "$f" ] && echo "$(basename "$f") = $(cat "$f" 2>/dev/null)"; done'
probe "fb devices"          ls -la /dev/fb*
probe "MOD_ROOT tree"       ls -la "$MOD_ROOT_DIR"
probe "MOD_ROOT/.shell"     ls -la "$MOD_ROOT_DIR/.shell"
probe "MOD chroot root"     ls -la "$MOD_DIR"
probe "MOD /bin"            ls -la "$MOD_DIR/bin"
probe "MOD /lib"            ls -la "$MOD_DIR/lib"

# The bash trampoline is a prime suspect for a pre-failsafe hang: it execs the
# chroot's ld.so against the chroot's bash. If any of these three is missing or
# is the wrong architecture, the hook dies or stalls before anything is logged.
probe "trampoline: bash"    ls -la "$MOD_DIR/bin/bash"
probe "trampoline: loaders" sh -c "ls -la $MOD_DIR/lib/ld-* 2>/dev/null"
probe "trampoline: file(1)" sh -c "command -v file >/dev/null 2>&1 && file $MOD_DIR/bin/bash $MOD_DIR/lib/ld-* 2>/dev/null || echo 'no file(1) on device'"

probe "usr/prog listing"    ls -la /usr/prog
probe "stock startup"       cat "$APP_STARTUP"
probe "variables.cfg"       cat "$MOD_ROOT_DIR/../mod_data/variables.cfg"
probe "existing mod logs"   sh -c "ls -la $FF_DATA_MNT/*.log $FF_DATA_MNT/logFiles 2>/dev/null; ls -la $MOD_ROOT_DIR/../mod_data/log 2>/dev/null"

for _l in /tmp/forgex.log /usr/data/forgex.log /usr/data/mod_data/log/init.log; do
    [ -f "$_l" ] && grab "$_l" "$(echo "$_l" | tr '/' '_').captured"
done

distribute_report

# --------------------------------------------------------------------------
# 3. MEASURE - the framebuffer facts our status frames keep getting wrong.
# --------------------------------------------------------------------------
say ""
say "[ad5x-debug] 3. MEASURE framebuffer"
_vsize=$(cat /sys/class/graphics/fb0/virtual_size 2>/dev/null)
_bpp=$(cat /sys/class/graphics/fb0/bits_per_pixel 2>/dev/null)
_stride=$(cat /sys/class/graphics/fb0/stride 2>/dev/null)
say "  virtual_size   = ${_vsize:-<unreadable>}"
say "  bits_per_pixel = ${_bpp:-<unreadable>}"
say "  stride         = ${_stride:-<unreadable>}"
# gen_fb_frames.py hardcodes 480x800 portrait. ZMOD's own AD5X assets say the
# panel is 800x480 landscape, and both geometries are 1,536,000 bytes at 32bpp,
# so the write succeeds and the picture shears instead of failing loudly. This
# probe is what settles it on the actual hardware.
say "  frames are generated as 480x800; ZMOD's AD5X assets say 800x480."
say "  virtual_size above is the authority - it decides which one is right."

# A real capture of what the panel is showing beats any assumption about format.
for _d in $OUT_DIRS; do
    dd if="$FF_FB" of="$_d/fb0-dump.raw" bs=1024 count=4096 2>/dev/null && \
        say "  dumped live framebuffer -> $_d/fb0-dump.raw"
    break
done

# --------------------------------------------------------------------------
# 4. ARM - a traced, timeout-guarded runner for interactive use over SSH.
#    Not wired into boot: nothing here can wedge the printer again.
# --------------------------------------------------------------------------
say ""
say "[ad5x-debug] 4. ARM traced runner"
_runner="$FF_DATA_MNT/forgex-debug/run-traced.sh"
mkdir -p "$FF_DATA_MNT/forgex-debug" 2>/dev/null
cat > "$_runner" <<'RUNNER'
#!/bin/sh
# Reproduce the AD5X bring-up with a full shell trace and a hard timeout.
# Run this over SSH once the printer is up on stock firmware:
#     sh /usr/data/forgex-debug/run-traced.sh [seconds]
# The trace lands next to this script. The timeout means a hang ends the run
# instead of the boot, so the LAST LINE of the trace is the hanging command.
BOOT=/usr/data/config/mod/.shell/ad5x_bootstrap.sh
LIMIT="${1:-120}"
OUT="/usr/data/forgex-debug/trace-$(date '+%Y%m%d-%H%M%S' 2>/dev/null || echo now).log"
[ -x "$BOOT" ] || { echo "bootstrap missing: $BOOT"; exit 1; }
echo "tracing $BOOT for up to ${LIMIT}s -> $OUT"
# `sh -x` rather than editing the script: the trampoline re-execs under the
# chroot bash, so tracing the outer sh still shows exactly where it stops.
( sh -x "$BOOT" run ) > "$OUT" 2>&1 &
_pid=$!
_n=0
while [ "$_n" -lt "$LIMIT" ]; do
    kill -0 "$_pid" 2>/dev/null || break
    sleep 1
    _n=$((_n + 1))
done
if kill -0 "$_pid" 2>/dev/null; then
    echo "TIMED OUT after ${LIMIT}s - killing. Last trace lines:"
    kill -9 "$_pid" 2>/dev/null
    echo "--- HUNG AT ---" >> "$OUT"
else
    echo "completed on its own."
fi
tail -40 "$OUT"
echo ""
echo "full trace: $OUT"
RUNNER
chmod +x "$_runner" 2>/dev/null
say "  wrote $_runner"
say "  once stock is up:  sh $_runner 120"

for _d in $OUT_DIRS; do
    cp "$_runner" "$_d/run-traced.sh" 2>/dev/null
done

# --------------------------------------------------------------------------
# 5. SIGNAL - solid white means finished. No reboot; pull the stick.
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 6. SELF-DELETE from the stick.
#
# The stock updater re-runs whatever payload it finds on the stick on EVERY
# boot. That, plus the release installer's unconditional sysrq reboot, is what
# made the install loop - ZMOD avoids it by removing its own image before
# finishing (rm -f /mnt/AD5X-zmod-*). This stick installs nothing and never
# reboots, so it cannot loop, but leaving it runnable means it re-runs on every
# power cycle until the stick comes out. Remove ourselves for the same reason.
# --------------------------------------------------------------------------
say ""
say "[ad5x-debug] 6. Removing the payload from the USB stick"
for _mp in $(mount 2>/dev/null | awk '/vfat|msdos|fuseblk/ {print $3}') /mnt; do
    [ -d "$_mp" ] || continue
    for _img in "$_mp"/AD5X-*.tgz "$_mp"/AD5X-*.tar; do
        if [ -f "$_img" ]; then
            rm -f "$_img" 2>/dev/null && say "  removed $_img"
        fi
    done
done
sync

# --------------------------------------------------------------------------
# 7. SIGNAL - solid white means finished. No reboot; pull the stick.
# --------------------------------------------------------------------------
distribute_report

say ""
if [ "$RESCUE_OK" = yes ]; then
    say "[ad5x-debug] DONE - hook removed. Screen goes SOLID WHITE."
    say "[ad5x-debug] Pull the stick, then power cycle: it will boot pure stock."
    sync
    fb_fill '\377'
else
    say "[ad5x-debug] DONE - but the RESCUE FAILED (hook still wired)."
    say "[ad5x-debug] Screen goes SOLID DARK GREY. It will hang again on boot."
    say "[ad5x-debug] Diagnostics were still collected; pull the stick and report."
    sync
    fb_fill '\044'
fi
distribute_report
sync

exit 0
