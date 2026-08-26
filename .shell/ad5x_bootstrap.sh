#!/bin/bash

## AD5X headless boot bootstrap
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

## The AD5X boots one monolithic stock /usr/prog/app_startup.sh off a read-only
## squashfs; nothing auto-runs /etc/init.d, so Forge-X's S00init -> S99root
## chain never fires the way it does on the AD5M. This script is the single hook
## we inject into that stock file to drive Forge-X's orchestration imperatively,
## calling only the MIPS-safe stages. S55boot/boot_mcu/netd/zstart_klipper/
## init_swap/tone.py are deliberately never invoked: they are ARM ELFs or would
## double-launch klippy (stock's own klipper/start.sh owns that launch).
##
## Subcommands:
##   install    inject the hook into stock app_startup.sh (idempotent, reversible)
##   uninstall  remove the hook / restore the .orig backup (idempotent)
##   run        (default) the ordered headless bring-up; --dry-run prints the plan

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
# explicit choice - and every other key - is left untouched.
_ensure_var() {
    local key="$1" val="$2" cur
    cur="$("$CMDS"/zconf.sh "$VAR_PATH" --get "$key" "__MISSING__")"
    if [ "$cur" = "__MISSING__" ]; then
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

failsafe_disarm() {
    rm -f "$BOOT_FAILURE_F"
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

    if [ "$dry_run" -eq 1 ]; then
        echo "[dry-run] AD5X headless bootstrap plan (PLATFORM=$PLATFORM):"
        echo "[dry-run] failsafe: stand down this boot if BOOT_FLAG_FAILURE/SKIP is set; otherwise arm BOOT_FLAG_FAILURE before step 2 and clear it after step 8"
        echo "[dry-run] 1. source platform.sh, common.sh, klipper_overlay.sh, init_lib.sh"
        echo "[dry-run] 2. preconditions: bind /opt->$DATA_MNT and /data->$DATA_MNT; move aside \$MOD/ZMOD marker; ensure variables.cfg display=HEADLESS, use_swap=OFF; stop stock UI ($STOCK_UI_PROCS)"
        echo "[dry-run] 3. mount_data_partition"
        echo "[dry-run] 4. init_buildroot"
        echo "[dry-run] 5. apply_klipper_patches"
        echo "[dry-run] 6. fix_config"
        echo "[dry-run] 7. launch klipper (zstart_klipper.sh against the patched /usr/prog/klipper)"
        echo "[dry-run] 8. first-run database (migrate_db + restore_ota) then chroot start.sh (ntpd, Moonraker :7125, httpd :80)"
        echo "[dry-run] 9. done; stock app_startup.sh continues (it does not launch klippy)"
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
        return 0
    fi
    failsafe_arm

    # Step 2. Preconditions no installer set up on the AD5X.
    echo "// [ad5x] Establishing preconditions..."
    # The boot scripts carry ~140 /opt/config/mod literals by design; the /opt
    # bind resolves them to $MOD_ROOT. init_buildroot also has two raw /data
    # literals the /opt bind does not cover, so bind /data too.
    _ensure_bind "$DATA_MNT" /opt
    _ensure_bind "$DATA_MNT" /data
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

    # Stock app_startup.sh launches the Qt UI (firmwareExe) before our hook runs.
    # Stop it: headless owns the framebuffer, and a live firmwareExe also owns the
    # MCU/klippy we are about to take over. STOCK_UI_PROCS is from platform.sh.
    echo "// [ad5x] Stopping stock UI ($STOCK_UI_PROCS)..."
    for _proc in $STOCK_UI_PROCS; do
        killall "$_proc" >/dev/null 2>&1 || true
    done

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
        return 1
    fi

    # Step 6. Write printer.cfg (display resolves to init.display.headless.cfg).
    echo "// [ad5x] Restoring config..."
    fix_config

    # Step 7. Launch klippy. Stock app_startup.sh never launches it (unlike the
    # AD5M, where S55boot/boot.sh runs zstart_klipper), so the mod owns the launch
    # - the same MIPS-safe launcher, run against the just-patched /usr/prog/klipper
    # through the host Python env in its start.sh. klipperDaemon backgrounds, so
    # this returns and moonraker (Step 8) connects to it.
    echo "// [ad5x] Launching klipper..."
    "$CMDS"/zstart_klipper.sh > /dev/null 2>&1

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

    # Bring-up completed: clear the failure flag so the next boot re-arms the mod.
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
