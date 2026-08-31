#!/bin/bash

## Shared Buildroot/config initialization stages
##
## Copyright (C) 2025-2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

## init_buildroot and fix_config were extracted verbatim from S00init so that
## the AD5X boot bootstrap (.shell/ad5x_bootstrap.sh) can call the exact same
## stages without sourcing S00init, whose bottom `case` dispatch runs
## initialize()/exit on any argument (there is no neutral one to source with).
## S00init sources this file and keeps calling both functions unchanged.

# Board-specific values and init_chroot come from common.sh (which sources the
# descriptor). Path is relative to this file so it resolves on-device and when a
# test or the bootstrap sources it from the checkout.
# shellcheck disable=SC1090,SC1091
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Map a host path under $DATA_MNT to where that same partition is visible inside
# the chroot: init_buildroot bind-mounts $DATA_MNT at $MOD/data, so the chroot
# spelling is /data plus whatever followed $DATA_MNT. /usr/data/gcodes ->
# /data/gcodes on the AD5X; on the AD5M $DATA_MNT is already /data, so /data and
# /data/logFiles pass through unchanged.
_chroot_data_path() {
    echo "/data${1#"$DATA_MNT"}"
}

# Build the printer_data tree at $1. The symlink targets are chroot-relative on
# both boards - init_buildroot binds $DATA_MNT->$MOD/data and /opt/config->
# $MOD/opt/config - so the contents are identical regardless of where the tree
# is built; only provision_printer_data's choice of location differs per board.
_init_printer_data() {
    local pd="$1"

    mkdir -p "$pd" "$pd"/certs "$pd"/comms "$pd"/misc "$pd"/tmp

    # $GCODE_DIR/$LOG_DIR are host paths the chroot cannot see at their host
    # spelling; _chroot_data_path re-homes them under the /data bind so Moonraker
    # (in the chroot) and klippy (on the host) agree. On the AD5M both resolve to
    # the exact strings these symlinks always had (/data and /data/logFiles).
    ln -fns "$(_chroot_data_path "$GCODE_DIR")" "$pd"/gcodes
    ln -fns "$(_chroot_data_path "$LOG_DIR")" "$pd"/logs
    ln -fns /opt/config/ "$pd"/config
    ln -fns /opt/config/mod_data/database "$pd"/database
    ln -fns /opt/config/mod/.bin/exec "$pd"/bin
    ln -fns /opt/config/mod/.py "$pd"/py
    ln -fns /opt/config/mod/.shell "$pd"/scripts

    ln -fns /opt/config/mod/moonraker.conf "$pd"/config/moonraker.conf
    ln -fns /opt/config/mod/.root/moonraker.asvc "$pd"/moonraker.asvc
}

# Provision printer_data in the right place for the board. The AD5M builds it on
# the host /root and bind-mounts it into the chroot: host-side tools (zmem/zsend/
# zfs) read the host copy, so it must exist there. The AD5X host / is a
# read-only squashfs with no writable /root, so there is nowhere on the host to
# build - it goes straight into the chroot's own writable /root, with no bind.
provision_printer_data() {
    if [ "$PLATFORM" = ad5x ]; then
        _init_printer_data "$MOD"/root/printer_data
        # klippy runs on the host, whose / is a read-only squashfs: /root is
        # empty and no /root/printer_data can be created there, yet Klipper's
        # config references /root/printer_data by absolute path (the
        # gcode_shell_command command: paths, etc.). Bind the chroot's /root -
        # which holds the tree just built - over the host's empty /root, so host
        # klippy and the chroot's Moonraker share one printer_data tree. This is
        # the AD5M's single-tree sharing in reverse: the AD5M builds on the host
        # and binds into the chroot; the AD5X builds in the chroot and binds onto
        # the host, because the writable side is flipped.
        mount --bind "$MOD"/root /root
    else
        _init_printer_data /root/printer_data
        sync
        mount --bind /root/printer_data "$MOD"/root/printer_data
    fi
}

# Klipper's [include] cannot pick a file by platform, so board-specific macro
# overrides ship as macros/<name>.$PLATFORM.cfg beside the default
# macros/<name>.cfg and are copied over it here, before Klipper reads them. The
# AD5M ships no .ad5m overrides, so this is a no-op there and the committed
# defaults stay in force; the AD5X copies its .ad5x variants (e.g.
# hw_base.ad5x.cfg, which rewrites the AD5M-frame motion geometry for this
# corner-origin machine).
apply_platform_macros() {
    local override base
    for override in "$MOD_ROOT"/macros/*."$PLATFORM".cfg; do
        [ -f "$override" ] || continue
        base="${override%."$PLATFORM".cfg}.cfg"
        cp -f "$override" "$base"
    done
}

# The AD5X host ships no /bin/bash: its / is a read-only squashfs (BusyBox only)
# and the kernel has no overlayfs, so a #!/bin/bash mod script - the Forge-X
# gcode_shell_command targets klippy runs on the host - cannot start. Provide
# /bin/bash without editing a single mod script: build a faithful superset of
# the host /bin (a byte-for-byte copy, plus the .shell/host-bin/bash trampoline
# that re-execs the mod's own bash via the rootfs loader) and bind it over /bin.
# Every mod script then runs unmodified with its pristine #!/bin/bash. The AD5M
# host already has /bin/bash, so this is a no-op there. Idempotent: a second
# call this boot - or a future host that ships bash - sees /bin/bash and returns
# before touching anything. HOST_BASH_PROBE lets the test suite force the build
# path; it is /bin/bash in every real run.
provide_host_bash() {
    [ "$PLATFORM" = ad5x ] || return 0
    [ -x "${HOST_BASH_PROBE:-/bin/bash}" ] && return 0

    rm -rf "$HOST_BIN_DIR"
    mkdir -p "$HOST_BIN_DIR"
    # cp -a preserves the setuid busybox binary and every applet symlink, so the
    # superset is functionally identical to the host /bin it stands in for.
    cp -a /bin/. "$HOST_BIN_DIR"/
    cp "$SCRIPTS"/host-bin/bash "$HOST_BIN_DIR"/bash
    chmod 0755 "$HOST_BIN_DIR"/bash

    mount --bind "$HOST_BIN_DIR" /bin
}

init_buildroot() {
    init_chroot

    mkdir -p "$MOD"/dev/pts
    mkdir -p "$MOD"/data
    mkdir -p "$MOD"/opt/config
    mkdir -p "$MOD"/opt/klipper
    mkdir -p "$MOD"/root/printer_data

    mount --bind "$DATA_MNT" "$MOD"/data
    mount --bind /opt/config "$MOD"/opt/config
    mount --bind "$KLIPPER_DIR" "$MOD"/opt/klipper

    # Init printer_data - on the host /root (AD5M) or in the chroot (AD5X). See
    # provision_printer_data.
    provision_printer_data

    echo "// Finishing buildroot initialization..."

    # hwclock
    ln -fs /opt/config/mod/.root/fake-hwclock "$MOD"/usr/sbin/

    # load datetime
    echo "// Loading last saved time..."
    chroot "$MOD" fake-hwclock load

    # moon
    ln -fns /opt/config/mod/.root/moonraker "$MOD"/root/moonraker-env/moonraker

    # web
    mkdir -p "$MOD"/root/www
    [ -d "$MOD"/root/fluidd ] && mv "$MOD"/root/fluidd "$MOD"/root/www/
    [ -d "$MOD"/root/mainsail ] && mv "$MOD"/root/mainsail "$MOD"/root/www/

    ln -fs /opt/config/mod/.root/config.json "$MOD"/root/www/

    "$CMDS"/zhttp.sh apply
    sync
}

fix_config() {
    TMP_CFG_PATH=/tmp/printer.tmp.cfg
    BATCH_FILE=/tmp/cfg_backup_batch.json

    # Select board-specific macro overrides before Klipper reads the tree.
    apply_platform_macros

    # Platform config overrides: prefer .cfg.$PLATFORM/<file> when it exists, else
    # the shared .cfg/<file>. Echoes the CHROOT path (cfg_backup runs in the
    # chroot); the existence test is on the host tree ($MOD_ROOT maps to the chroot
    # /opt/config/mod). A board with no override dir (e.g. AD5M) always gets .cfg/.
    _cfg_path() {
        if [ -f "$MOD_ROOT/.cfg.$PLATFORM/$1" ]; then
            echo "/opt/config/mod/.cfg.$PLATFORM/$1"
        else
            echo "/opt/config/mod/.cfg/$1"
        fi
    }

    # 1. Create dump with parameters from printer.base.cfg
    # Check if any parameters were found. cfg_backup.py runs INSIDE the chroot,
    # where the mod is always at /opt/config/mod (init_buildroot binds it there
    # on both boards). $PY is the host path ($MOD_ROOT/.py, i.e.
    # /usr/data/config/mod/.py on the AD5X) and is not visible in the chroot, so
    # the chroot-relative path is required - the sibling .root chroot calls use
    # the same /opt/config/mod prefix.
    if chroot "$MOD" /bin/python3 /opt/config/mod/.py/cfg_backup.py \
        --mode backup \
        --config /opt/config/printer.base.cfg \
        --data $TMP_CFG_PATH \
        --params "$(_cfg_path init.move.cfg)"; then
        # The dump holds only what the printer's own base cfg carries - a
        # param it lacks is absent rather than defaulted from the fallback
        # body. That is deliberate: the stock config is the authority on its
        # own hardware, and a named param missing from it is an anomaly to
        # surface, not to paper over with another machine's default. An
        # entirely empty dump already raises inside cfg_backup.
        DATA_MOVE_CFG=$TMP_CFG_PATH
    else
        DATA_MOVE_CFG="$(_cfg_path data.init.move.cfg)"
    fi

    # Create the batch file
    echo "[" > $BATCH_FILE

    # 2. Move params from printer.base.cfg to printer.cfg
    echo "
    {
        \"mode\": \"restore\",
        \"config\": \"/opt/config/printer.cfg\",
        \"data\": \"$DATA_MOVE_CFG\",
        \"params\": \"$(_cfg_path init.move.cfg)\",
        \"avoid_writes\": true
    }," >> $BATCH_FILE

    # 3. Initialize display configuration
    local screen
    screen="$("$SCRIPTS"/commands/zdisplay.sh test)"
    if [ "$screen" == "STOCK" ]; then
        # Stock screen enabled
        echo "
        {
            \"mode\": \"restore\",
            \"config\": \"/opt/config/printer.cfg\",
            \"params\": \"/opt/config/mod/.cfg/init.display.stock.cfg\",
            \"no_data\": true,
            \"avoid_writes\": true
        }," >> $BATCH_FILE
    elif [ "$screen" == "FEATHER" ]; then
        # Feather screen enabled
        echo "
        {
            \"mode\": \"restore\",
            \"config\": \"/opt/config/printer.cfg\",
            \"params\": \"/opt/config/mod/.cfg/init.display.feather.cfg\",
            \"no_data\": true,
            \"avoid_writes\": true
        }," >> $BATCH_FILE
    elif [ "$screen" == "HEADLESS" ]; then
        # Headless mode enabled
        echo "
        {
            \"mode\": \"restore\",
            \"config\": \"/opt/config/printer.cfg\",
            \"params\": \"/opt/config/mod/.cfg/init.display.headless.cfg\",
            \"no_data\": true,
            \"avoid_writes\": true
        }," >> $BATCH_FILE
    elif [ "$screen" == "GUPPY" ]; then
        # Guppy mode enabled
        echo "
        {
            \"mode\": \"restore\",
            \"config\": \"/opt/config/printer.cfg\",
            \"params\": \"/opt/config/mod/.cfg/init.display.guppy.cfg\",
            \"no_data\": true,
            \"avoid_writes\": true
        }," >> $BATCH_FILE
    else
        echo @@ Invalid display parameter: "$screen"
    fi

    # 4. Init printer.cfg configuration
    echo "
    {
        \"mode\": \"restore\",
        \"config\": \"/opt/config/printer.cfg\",
        \"params\": \"$(_cfg_path init.cfg)\",
        \"data\": \"$(_cfg_path data.init.cfg)\",
        \"avoid_writes\": true
    }," >> $BATCH_FILE

    # 5. Init printer.base.cfg configuration
    echo "
    {
        \"mode\": \"restore\",
        \"config\": \"/opt/config/printer.base.cfg\",
        \"params\": \"$(_cfg_path init.base.cfg)\",
        \"data\": \"$(_cfg_path data.init.base.cfg)\",
        \"avoid_writes\": true
    }," >> $BATCH_FILE

    # 6. Apply tunning parameters
    TUNING_ENABLED=$("$CMDS"/zconf.sh "$VAR_PATH" --get "tune_config" "0")
    if [ "$TUNING_ENABLED" -eq 1 ]; then
        echo "
        {
            \"mode\": \"restore\",
            \"config\": \"/opt/config/printer.cfg\",
            \"params\": \"/opt/config/mod/.cfg/tuning.cfg\",
            \"no_data\": true,
            \"avoid_writes\": true
        }," >> $BATCH_FILE
    else
         echo "
        {
            \"mode\": \"restore\",
            \"config\": \"/opt/config/printer.cfg\",
            \"params\": \"/opt/config/mod/.cfg/tuning.off.cfg\",
            \"no_data\": true,
            \"avoid_writes\": true
        }," >> $BATCH_FILE
    fi

    # 7. Restore printer.base.cfg if a backup exists
    if [ -f /opt/config/printer.base.cfg.bak ]; then
        echo "
        {
            \"mode\": \"restore\",
            \"config\": \"/opt/config/printer.base.cfg\",
            \"params\": \"/opt/config/mod_data/backup.params.cfg\",
            \"data\": \"/opt/config/printer.base.cfg.bak\",
            \"avoid_writes\": true
        }," >> $BATCH_FILE
    fi

    # Finalize the batch file (remove last comma and close array)
    sed -i '$s/,$//' $BATCH_FILE
    echo "]" >> $BATCH_FILE

    # Run the batch file
    # Chroot-relative path, as above: $PY is a host path invisible in the chroot.
    chroot "$MOD" /bin/python3 /opt/config/mod/.py/cfg_backup.py --batch $BATCH_FILE
    sync

    # Clean up the temporary files
    rm -f $BATCH_FILE $TMP_CFG_PATH
}
