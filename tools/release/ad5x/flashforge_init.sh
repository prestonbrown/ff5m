#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
#
## Forge-X AD5X (MIPS) Stage-B installer.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license.
##
## The stock FlashForge updater extracts our release archive and runs this file
## as its entry point, passing "MACHINE PID" as "$1 $2" (e.g. "AD5X 0026"). By
## the time we run, $WORK_DIR (the directory this script sits in) holds the
## seven release members at top level:
##
##     flashforge_init.sh  common.sh  version.txt  md5.list
##     xz/data.tar.xz  xz/buildroot.tar.xz  xz/entware.tar.xz
##
## We verify the payload (md5), guard the environment (right printer, mod not
## already running, enough free space), unpack the chroot rootfs + mod tree,
## wire the reversible boot hook into the stock app_startup.sh via the already
## built injector, defer the first bring-up to the next boot, remove our image
## from the stick, and stop. We deliberately DO NOT reboot: see
## remove_image_from_usb() for the install loop that behaviour created.
##
## This is the single most safety-critical file in the AD5X port. It runs ON
## THE PRINTER under BusyBox ash: POSIX sh only - no bash, no GNU coreutils.
## Every early failure exits 0 ON PURPOSE. A NONZERO exit makes the stock
## updater boot the stock firmware immediately; by swallowing the failure,
## painting an error frame, and returning 0 we let the (still unmodified)
## printer come back up stock on its own, rather than into a half state.
##
## Debugging: uncomment the next line to trace every command. NEVER commit it
## enabled - the committed installer must be quiet.
# set -x
set -e

# --------------------------------------------------------------------------
# Testability seams. Each defaults to its real on-device value and is only
# redirected when the test harness sets it, so they are inert in production.
#   FF_FB           framebuffer device the frames are painted to
#   FF_DATA_MNT     data-partition mount; unpack targets derive from it
#   FF_UNAME        overrides `uname -m` (arch guard)
#   FF_FREE_KB      overrides the measured free space in KiB (space guard)
#   FF_MOUNTS       file that stands in for `mount` output (already-running guard)
#   FF_NO_REBOOT    when set, suppress the sysrq reboot AND the pre-reboot sleep
#   FF_SKIP_INSTALL when set, stop right after the guards (before md5/unpack)
# --------------------------------------------------------------------------
: "${FF_FB:=/dev/fb0}"
: "${FF_DATA_MNT:=/usr/data}"

WORK_DIR=$(dirname "$0")
MACHINE=$1
PID=$2

# Unpack targets. On-device FF_DATA_MNT is /usr/data, so these resolve to the
# platform descriptor's $MOD (chroot rootfs) and $MOD_ROOT (mod source tree).
MOD_DIR="$FF_DATA_MNT/.mod/.forge-x"       # $MOD      - Buildroot chroot rootfs
MOD_ROOT_DIR="$FF_DATA_MNT/config/mod"     # $MOD_ROOT - mod .shell/.py/.root tree
BOOTSTRAP="$MOD_ROOT_DIR/.shell/ad5x_bootstrap.sh"

# 512 MB in KiB - the free space we insist on before touching the disk.
MIN_SPACE=524228

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

# Paint one xz-compressed 480x800@32bpp raw framebuffer dump from img/. Missing
# frame or absent fb must never abort the install, hence 2>/dev/null || true.
paint() {
    xzcat "$WORK_DIR/img/$1" > "$FF_FB" 2>/dev/null || true
}

# Machine architecture, overridable for the off-rig guard test.
uname_m() {
    if [ -n "${FF_UNAME:-}" ]; then
        echo "$FF_UNAME"
    else
        uname -m
    fi
}

# The active mount table, overridable so the already-running guard is testable
# without actually mounting anything.
current_mounts() {
    if [ -n "${FF_MOUNTS:-}" ]; then
        cat "$FF_MOUNTS" 2>/dev/null || true
    else
        mount
    fi
}

# Free KiB on the data partition, overridable for the low-space guard test.
free_kb() {
    if [ -n "${FF_FREE_KB:-}" ]; then
        echo "$FF_FREE_KB"
    else
        df "$FF_DATA_MNT" | tail -1 | tr -s ' ' | cut -d' ' -f4
    fi
}

# Remove our own image from the USB stick.
#
# The stock updater re-reads the stick on EVERY boot, so an image left in place
# reinstalls forever. That, combined with the unconditional sysrq reboot this
# installer used to end on, was an airtight install loop: reboot -> updater
# finds the same .tgz -> installs again -> reboots. The "mod already running"
# guard could never break it either, because the reboot happened before
# app_startup.sh ever reached our boot hook, so no .forge-x mount ever existed.
#
# ZMOD solves it exactly this way (rm -f /mnt/AD5X-zmod-*); the port dropped it.
# The stick is mounted at /mnt on this platform.
remove_image_from_usb() {
    _removed=0
    # current_mounts() honours FF_MOUNTS, so a test drives this against a
    # fixture instead of whatever the build host happens to have mounted.
    for _mp in $(current_mounts 2>/dev/null | awk '/vfat|msdos/ {print $2}'); do
        [ -d "$_mp" ] || continue
        for _img in "$_mp"/AD5X-*.tgz "$_mp"/AD5X-*.tar; do
            if [ -f "$_img" ]; then
                rm -f "$_img" 2>/dev/null && echo "[ad5x-install] Removed $_img" \
                    && _removed=$((_removed + 1))
            fi
        done
    done
    sync
    [ "$_removed" -gt 0 ] || echo "[ad5x-install] No image found on USB to remove."
}

# Stand the mod down for the REST OF THIS BOOT.
#
# Without the reboot, stock app_startup.sh carries on in the same boot and would
# reach the hook we just injected - running a first bring-up against a mod tree
# unpacked seconds earlier, mid-update, with the stock UI still coming up. The
# one-shot skip flag defers that to a clean power cycle. failsafe_should_skip()
# consumes the flag on that pass, so the NEXT boot is a normal first run.
defer_first_bringup() {
    if mkdir -p "$MOD_ROOT_DIR" 2>/dev/null \
       && touch "$MOD_ROOT_DIR/BOOT_FLAG_SKIP" 2>/dev/null; then
        echo "[ad5x-install] Deferred first bring-up to the next boot."
    else
        echo "[ad5x-install] WARNING: could not write BOOT_FLAG_SKIP;" \
             "the mod may start before the power cycle."
    fi
}

# --------------------------------------------------------------------------
# guards - every failure here paints a frame and exits 0 (see header)
# --------------------------------------------------------------------------
guard_environment() {
    _arch=$(uname_m)

    # Guard 1 - only the AD5X (MACHINE/PID) and only real MIPS silicon.
    if [ "$MACHINE" != "AD5X" ] || [ "$PID" != "0026" ] || [ "$_arch" != "mips" ]; then
        echo "[ad5x-install] Refusing: update does not match this machine" \
             "(MACHINE='$MACHINE' PID='$PID' ARCH='$_arch')."
        paint forgex-error.raw.xz
        # exit 0: a nonzero exit would make the stock updater boot stock now.
        exit 0
    fi

    # Guard 2 - refuse if a mod (Forge-X or ZMOD) is already mounted/running.
    # Re-installing over a live mod would race its running services.
    if current_mounts | grep -q '\.forge-x' || current_mounts | grep -q 'zmod'; then
        echo "Mod already running; installation prohibited."
        exit 1
    fi

    # Guard 3 - require 512 MB free before we write anything. Coerce a
    # non-numeric df reading to 0 so an unreadable partition fails safe here.
    _free=$(free_kb)
    case "$_free" in
        '' | *[!0-9]*) _free=0 ;;
    esac
    if [ "$_free" -lt "$MIN_SPACE" ]; then
        echo "[ad5x-install] Refusing: only ${_free} KiB free on $FF_DATA_MNT," \
             "need ${MIN_SPACE} KiB (512 MB)."
        paint forgex-nospace.raw.xz
        exit 0
    fi
}

# --------------------------------------------------------------------------
# payload verification
# --------------------------------------------------------------------------
verify_payload() {
    # md5sum -c resolves the listed paths relative to its cwd, so run it from
    # $WORK_DIR where xz/*.tar.xz and the other members live.
    if ! ( cd "$WORK_DIR" && md5sum -c md5.list ); then
        echo "[ad5x-install] Refusing: payload md5 verification failed."
        paint forgex-error.raw.xz
        exit 0
    fi
    echo "[ad5x-install] Installing Forge-X version:" \
         "$(cat "$WORK_DIR/version.txt" 2>/dev/null || true)"
}

# --------------------------------------------------------------------------
# unpack + wire the boot hook
# --------------------------------------------------------------------------
install_payload() {
    # Chroot rootfs ($MOD): the second Buildroot root the mod enters with chroot.
    echo "[ad5x-install] Unpacking Buildroot rootfs -> $MOD_DIR"
    mkdir -p "$MOD_DIR"
    xz -dc "$WORK_DIR/xz/buildroot.tar.xz" | tar -xf - -C "$MOD_DIR"

    # Mod source tree ($MOD_ROOT): the .shell/.py/.root checkout the mod runs from.
    echo "[ad5x-install] Unpacking mod tree -> $MOD_ROOT_DIR"
    mkdir -p "$MOD_ROOT_DIR"
    xz -dc "$WORK_DIR/xz/data.tar.xz" | tar -xf - -C "$MOD_ROOT_DIR"

    # Entware (/opt) is optional. The first AD5X proofs ship an empty stub, so
    # key off whether the member actually has content rather than hardcoding
    # "skip on AD5X" - a real mipsel Entware later then unpacks with no change.
    if [ -s "$WORK_DIR/xz/entware.tar.xz" ]; then
        echo "[ad5x-install] Unpacking Entware -> /opt"
        mkdir -p /opt
        xz -dc "$WORK_DIR/xz/entware.tar.xz" | tar -xf - -C /opt
    else
        echo "[ad5x-install] Entware stub is empty; skipping."
    fi
    sync

    # Wire the boot hook through the already-built reversible injector rather
    # than open-coding sed edits into the stock startup here. It injects a
    # single '# forge-x'-sentinel line into stock /usr/prog/app_startup.sh,
    # idempotently, and keeps a .orig for a byte-identical uninstall.
    if [ -x "$BOOTSTRAP" ]; then
        echo "[ad5x-install] Wiring boot hook via $BOOTSTRAP"
        "$BOOTSTRAP" install
    else
        echo "[ad5x-install] WARNING: boot hook injector missing at" \
             "$BOOTSTRAP; boot hook NOT wired."
    fi
    sync
}

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
main() {
    guard_environment
    echo "[ad5x-install] Environment guards passed."

    # Test seam: stop after the guards, before any md5 check or unpack.
    if [ -n "${FF_SKIP_INSTALL:-}" ]; then
        echo "[ad5x-install] FF_SKIP_INSTALL set; stopping after guards."
        exit 0
    fi

    verify_payload

    paint forgex-install.raw.xz
    install_payload

    echo "[ad5x-install] Forge-X installed successfully."

    # Order matters: defer the bring-up BEFORE the stock startup can reach the
    # hook, and clear the stick BEFORE telling the user it is safe to pull.
    defer_first_bringup
    remove_image_from_usb

    echo "[ad5x-install] Remove the USB drive, then power cycle the printer."
    paint forgex-complete.raw.xz
    sync
    exit 0
}

main
