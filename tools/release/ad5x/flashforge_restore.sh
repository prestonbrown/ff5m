#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
#
## Forge-X AD5X STOCK RESTORE stick.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license.
##
## Undoes what the bring-up did to the STOCK firmware, so the printer boots on
## its own again. Installs nothing, never reboots.
##
## Why this is needed: disarming the boot hook stops the mod from RUNNING, but
## the bring-up had already reached apply_klipper_patches, and on the AD5X
## KLIPPER_DIR is /usr/prog/klipper - the stock klipper, on a writable ext4
## partition. That step replaces stock klippy modules with symlinks into
## /usr/data/config/mod/.py/klipper, saving each original as <file>.bak. Those
## symlinks outlive the mod, so stock klippy then imports patched modules whose
## runtime environment no longer exists: it fails to start, the UI sits on
## "Initializing..." and the board alarms.
##
## Order:
##   0. COLLECT  - klippy logs and the exact overlay state, BEFORE changing
##                 anything, so a failed restore is still diagnosable.
##   1. RESTORE  - every symlink into the mod tree goes back to its .bak, or is
##                 removed when no backup exists.
##   2. CONFIG   - put back a stock printer.cfg if a backup is present.
##   3. VERIFY   - re-scan and report what, if anything, is still overlaid.
##   4. SIGNAL   - white = stock klipper is clean, grey = something remains.
##
## No `set -e`: a failed probe must never abort the restore.

FF_DATA_MNT="${FF_DATA_MNT:-/usr/data}"
FF_FB="${FF_FB:-/dev/fb0}"
KLIPPER_DIR="${FF_KLIPPER_DIR:-/usr/prog/klipper}"
MOD_ROOT_DIR="$FF_DATA_MNT/config/mod"
CONFIG_DIR="$FF_DATA_MNT/config"
MOD_DATA_DIR="$FF_DATA_MNT/config/mod_data"
APP_STARTUP="${FF_APP_STARTUP:-/usr/prog/app_startup.sh}"
BOOTSTRAP_SH="$MOD_ROOT_DIR/.shell/ad5x_bootstrap.sh"
WORK_DIR=$(dirname "$0")
STAMP="$(date '+%Y%m%d-%H%M%S' 2>/dev/null || echo unknown)-$$"

OUT_DIRS=""
_add_out() {
    _d="$1"
    [ -n "$_d" ] || return 0
    mkdir -p "$_d" 2>/dev/null || return 0
    if touch "$_d/.wtest" 2>/dev/null; then
        rm -f "$_d/.wtest" 2>/dev/null
        OUT_DIRS="$OUT_DIRS $_d"
    fi
}
_add_out "$WORK_DIR/forgex-restore"
_add_out "$FF_DATA_MNT/forgex-restore"
for _mp in $(mount 2>/dev/null | awk '/vfat|msdos|fuseblk/ {print $3}'); do
    _add_out "$_mp/forgex-restore"
done

REPORT=""
for _d in $OUT_DIRS; do REPORT="$_d/restore-$STAMP.txt"; break; done
[ -n "$REPORT" ] || REPORT=/tmp/restore-$STAMP.txt

say() { echo "$@"; echo "$@" >> "$REPORT" 2>/dev/null; }
note() { echo "$@" >> "$REPORT" 2>/dev/null; }

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

# Every symlink under $KLIPPER_DIR that points into the mod tree.
overlaid_links() {
    find "$KLIPPER_DIR" -type l 2>/dev/null | while read -r _l; do
        _t=$(readlink "$_l" 2>/dev/null)
        case "$_t" in
            "$MOD_ROOT_DIR"/*|/usr/data/config/mod/*|/opt/config/mod/*) echo "$_l -> $_t" ;;
        esac
    done
}

say "[ad5x-restore] Forge-X AD5X stock-restore stick  $STAMP  arch=$(uname -m 2>/dev/null)"
say "[ad5x-restore] klipper dir: $KLIPPER_DIR"
say "[ad5x-restore] sinks:$OUT_DIRS"
say "[ad5x-restore] INSTALLS NOTHING, NEVER REBOOTS."

# --------------------------------------------------------------------------
# 0. COLLECT first. If the restore does not fix it, this is what explains why.
# --------------------------------------------------------------------------
say ""
say "[ad5x-restore] 0. COLLECT (before touching anything)"

note ""; note "===== overlaid symlinks BEFORE ====="
overlaid_links >> "$REPORT" 2>/dev/null
_before=$(overlaid_links | wc -l)
say "  stock klippy files currently overlaid: $_before"

note ""; note "===== .bak backups present ====="
find "$KLIPPER_DIR" -name '*.bak' 2>/dev/null >> "$REPORT"
say "  .bak backups found: $(find "$KLIPPER_DIR" -name '*.bak' 2>/dev/null | wc -l)"

# The logs that say WHY stock is stuck. Grab whatever exists.
# $FF_DATA_MNT/logs is where stock klippy actually logs on this board: stock
# app_startup.sh opens with `rm /usr/data/logs/printer.log*`, and ZMOD's klippy
# launcher sets KLIPPER_LOG=/usr/data/logs/printer.log. printer_data/ is the mod
# compat tree and logFiles/ is the AD5M path; keep both, but they are not where
# a stock-only boot writes.
for _log in \
    "$MOD_DATA_DIR"/log/ad5x_bootstrap.log \
    "$MOD_DATA_DIR"/log/ad5x_bootstrap.log.1 \
    "$MOD_DATA_DIR"/log/ad5x_bootstrap.log.2 \
    "$MOD_DATA_DIR"/log/*.log \
    "$FF_DATA_MNT"/logs/printer.log \
    "$FF_DATA_MNT"/logs/*.log \
    "$FF_DATA_MNT"/printer_data/logs/*.log \
    /usr/prog/printer_data/logs/*.log \
    /tmp/klippy.log /tmp/*.log \
    /usr/data/logFiles/*.log
do
    [ -f "$_log" ] || continue
    for _d in $OUT_DIRS; do
        cp "$_log" "$_d/log-$(echo "$_log" | tr '/' '_')" 2>/dev/null
    done
    note ""; note "===== tail: $_log ====="
    tail -120 "$_log" >> "$REPORT" 2>/dev/null
    say "  captured log $_log"
done

# The AD5X keeps its config in $FF_DATA_MNT/config, and nowhere else. Three
# independent confirmations: the FlashForge factory installer copies its
# other/printer.cfg to /usr/data/config/; ZMOD's klipper launcher passes
# KLIPPER_CONF=/usr/data/config/printer.cfg; and the mod's own fix_config writes
# /opt/config/printer.cfg, which is the same directory once Step 2 binds
# $DATA_MNT onto /opt. An earlier probe list guessed printer_data/config and
# found nothing - that was the probe being wrong, not the config being absent.
# The bring-up log in full - it is short, and the LAST line is the answer.
if [ -f "$MOD_DATA_DIR/log/ad5x_bootstrap.log" ]; then
    note ""; note "===== FULL BRING-UP LOG ====="
    cat "$MOD_DATA_DIR/log/ad5x_bootstrap.log" >> "$REPORT" 2>/dev/null
    say "  *** bring-up log captured: $(wc -l < "$MOD_DATA_DIR/log/ad5x_bootstrap.log" 2>/dev/null) lines"
    say "  *** last line: $(tail -1 "$MOD_DATA_DIR/log/ad5x_bootstrap.log" 2>/dev/null)"
else
    say "  !! no bring-up log at $MOD_DATA_DIR/log/ad5x_bootstrap.log"
fi

note ""; note "===== app_startup.sh hook state ====="
grep -n 'forge-x' "$APP_STARTUP" >> "$REPORT" 2>&1
for _f in "$APP_STARTUP" "$APP_STARTUP.orig"; do
    [ -f "$_f" ] || continue
    note "--- $_f ($(wc -c < "$_f") bytes) ---"
    for _d in $OUT_DIRS; do cp "$_f" "$_d/$(basename "$_f")" 2>/dev/null; done
done

note ""; note "===== printer.cfg candidates ====="
for _c in "$FF_DATA_MNT"/config/printer.cfg "$FF_DATA_MNT"/config/printer.base.cfg \
          /opt/config/printer.cfg "$FF_DATA_MNT"/printer_data/config/printer.cfg; do
    [ -f "$_c" ] || continue
    note "--- $_c ---"; head -40 "$_c" >> "$REPORT" 2>/dev/null
    for _d in $OUT_DIRS; do
        cp "$_c" "$_d/cfg-$(echo "$_c" | tr '/' '_')" 2>/dev/null
    done
    say "  captured config $_c"
done

note ""; note "===== processes ====="; ps >> "$REPORT" 2>&1
note ""; note "===== dmesg tail ====="; dmesg 2>/dev/null | tail -200 >> "$REPORT" 2>&1
note ""; note "===== mounts ====="; mount >> "$REPORT" 2>&1
distribute "$REPORT"

# --------------------------------------------------------------------------
# 1. RESTORE the stock klipper tree.
#
# Mirrors klipper_overlay_restore_or_remove(): prefer a real backup file, and
# fall back to deleting the dangling symlink so python imports the stock module
# again rather than failing on a broken path.
# --------------------------------------------------------------------------
say ""
say "[ad5x-restore] 1. RESTORE stock klipper"
_restored=0
_removed=0
find "$KLIPPER_DIR" -type l 2>/dev/null | while read -r _link; do
    _tgt=$(readlink "$_link" 2>/dev/null)
    case "$_tgt" in
        "$MOD_ROOT_DIR"/*|/usr/data/config/mod/*|/opt/config/mod/*) ;;
        *) continue ;;
    esac
    for _b in "$_link.bak" "$_link.backup" "$_link.old" "$_link.orig"; do
        if [ -f "$_b" ] && [ ! -L "$_b" ]; then
            rm -f "$_link" 2>/dev/null && mv -f "$_b" "$_link" 2>/dev/null \
                && echo "RESTORED $_link" >> "$REPORT.ops"
            continue 2
        fi
    done
    rm -f "$_link" 2>/dev/null && echo "REMOVED  $_link" >> "$REPORT.ops"
done

# Orphaned backups: a .bak whose live file vanished entirely.
find "$KLIPPER_DIR" -name '*.bak' 2>/dev/null | while read -r _b; do
    _live="${_b%.bak}"
    if [ ! -e "$_live" ] && [ ! -L "$_live" ]; then
        mv -f "$_b" "$_live" 2>/dev/null && echo "RESTORED(orphan) $_live" >> "$REPORT.ops"
    fi
done

if [ -f "$REPORT.ops" ]; then
    _restored=$(grep -c '^RESTORED' "$REPORT.ops" 2>/dev/null)
    _removed=$(grep -c '^REMOVED' "$REPORT.ops" 2>/dev/null)
    note ""; note "===== restore operations ====="
    cat "$REPORT.ops" >> "$REPORT" 2>/dev/null
    rm -f "$REPORT.ops"
fi
say "  restored from backup: ${_restored:-0}"
say "  dangling links removed: ${_removed:-0}"
sync

# --------------------------------------------------------------------------
# 2. CONFIG - only if a stock backup is actually present. Never invent one.
# --------------------------------------------------------------------------
say ""
say "[ad5x-restore] 2. CONFIG"

# Reverting the klipper OVERLAY does not revert the CONFIG, and fix_config
# (bring-up Step 6) rewrites printer.cfg with mod includes that resolve only
# under the mod's /opt bind. Stock klippy then cannot parse its own config,
# exits, and the stock UI sits on "Initializing..." forever - indistinguishable
# from the overlay failure, and not fixed by undoing the overlay alone.
#
# Only touch a config that is actually mod-flavoured: a stock-loadable file must
# be left exactly as it is, because the user's own tuning lives in it.
cfg_is_mod_flavoured() {
    grep -q -e '/opt/config/mod' -e 'mod/\.cfg' -e 'mod_data' "$1" 2>/dev/null
}

restore_cfg() {
    _live="$1"; _ship="$2"; _label="$(basename "$_live")"

    if [ ! -f "$_live" ]; then
        say "  $_label: absent, nothing to restore"
        return 0
    fi
    if ! cfg_is_mod_flavoured "$_live"; then
        say "  $_label: already stock-loadable (no mod references); left alone"
        return 0
    fi
    say "  $_label: contains mod references - stock klippy cannot load it"

    # Keep what the mod left, so a wrong guess here is still reversible.
    cp "$_live" "$_live.forgex-was" 2>/dev/null

    # Ordered candidates. The feather-before-headless name is what the display
    # switch leaves behind, and on this printer it is often the only backup.
    for _b in "$_live.bak" "$_live.orig" "$_live.backup" \
              "$_live.feather-before-headless.bak" \
              "$_live".*.bak; do
        [ -f "$_b" ] || continue
        [ -L "$_b" ] && continue
        case "$_b" in *.forgex-was) continue ;; esac
        if cfg_is_mod_flavoured "$_b"; then
            say "    skip $(basename "$_b"): also mod-flavoured"
            continue
        fi
        cp "$_b" "$_live" 2>/dev/null \
            && say "    restored from $(basename "$_b")" && return 0
    done

    # Last resort: the pristine copy shipped on this stick, straight out of the
    # FlashForge factory image. Only reached when no usable backup exists.
    if [ -n "$_ship" ] && [ -f "$_ship" ]; then
        cp "$_ship" "$_live" 2>/dev/null \
            && say "    restored from the FACTORY copy shipped on this stick" \
            && return 0
    fi

    say "    !! no usable backup and no shipped copy; left as-is"
    return 1
}

restore_cfg "$CONFIG_DIR/printer.cfg"      "$WORK_DIR/cfg/printer.cfg"
restore_cfg "$CONFIG_DIR/printer.base.cfg" "$WORK_DIR/cfg/printer.base.cfg"
sync

# --------------------------------------------------------------------------
# 2b. APP_STARTUP - take the hook out of the stock file entirely.
#
# Leaving the bootstrap non-executable already neuters the hook (it is guarded
# by `[ -x ... ]`), but restoring the pristine .orig removes any doubt and puts
# the stock file back byte-for-byte.
# --------------------------------------------------------------------------
say ""
say "[ad5x-restore] 2b. APP_STARTUP"
if [ -f "$APP_STARTUP.orig" ]; then
    cp "$APP_STARTUP" "$APP_STARTUP.forgex-was" 2>/dev/null
    if cat "$APP_STARTUP.orig" > "$APP_STARTUP" 2>/dev/null; then
        rm -f "$APP_STARTUP.orig"
        say "  restored $APP_STARTUP from .orig ($(wc -c < "$APP_STARTUP") bytes)"
    else
        say "  !! could not restore $APP_STARTUP from .orig"
    fi
elif grep -q 'forge-x' "$APP_STARTUP" 2>/dev/null; then
    # No .orig: strip exactly the sentinel-marked line.
    grep -v 'forge-x' "$APP_STARTUP" > "$APP_STARTUP.tmp" 2>/dev/null \
        && cat "$APP_STARTUP.tmp" > "$APP_STARTUP" 2>/dev/null \
        && rm -f "$APP_STARTUP.tmp" \
        && say "  stripped the hook line from $APP_STARTUP"
else
    say "  no hook present; nothing to do"
fi
sync

# --------------------------------------------------------------------------
# 3. VERIFY - re-scan. This decides the screen colour.
# --------------------------------------------------------------------------
say ""
say "[ad5x-restore] 3. VERIFY"
_after=$(overlaid_links | wc -l)
note ""; note "===== overlaid symlinks AFTER ====="
overlaid_links >> "$REPORT" 2>/dev/null
say "  overlaid before: $_before"
say "  overlaid after : $_after"

# Keep the mod disarmed regardless.
chmod a-x "$BOOTSTRAP_SH" 2>/dev/null
mkdir -p "$MOD_ROOT_DIR" 2>/dev/null && touch "$MOD_ROOT_DIR/BOOT_FLAG_SKIP" 2>/dev/null
rm -f "$MOD_ROOT_DIR/BOOT_FLAG_FAILURE" 2>/dev/null
say "  bootstrap left non-executable: $( [ -x "$BOOTSTRAP_SH" ] && echo NO || echo yes )"

for _mp in $(mount 2>/dev/null | awk '/vfat|msdos|fuseblk/ {print $3}') /mnt; do
    [ -d "$_mp" ] || continue
    for _img in "$_mp"/AD5X-*.tgz "$_mp"/AD5X-*.tar; do
        [ -f "$_img" ] && rm -f "$_img" 2>/dev/null && say "  removed $_img"
    done
done

distribute "$REPORT"
sync

say ""
if [ "${_after:-1}" -eq 0 ]; then
    say "[ad5x-restore] DONE - stock klipper is clean. SOLID WHITE."
    say "[ad5x-restore] Pull the stick and power cycle."
    fb_fill '\377'
else
    say "[ad5x-restore] DONE - $_after overlaid file(s) REMAIN. SOLID DARK GREY."
    say "[ad5x-restore] Pull the stick and bring it back; a stock reflash may be needed."
    fb_fill '\044'
fi
distribute "$REPORT"
sync

exit 0
