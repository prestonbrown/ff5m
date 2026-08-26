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

init_buildroot() {
    init_chroot

    mkdir -p "$MOD"/dev/pts
    mkdir -p "$MOD"/data
    mkdir -p "$MOD"/opt/config
    mkdir -p "$MOD"/opt/klipper
    mkdir -p "$MOD"/root/printer_data

    mount --bind /data "$MOD"/data
    mount --bind /opt/config "$MOD"/opt/config
    mount --bind "$KLIPPER_DIR" "$MOD"/opt/klipper

    mkdir -p /root/printer_data
    mkdir -p /root/printer_data/certs
    mkdir -p /root/printer_data/comms
    mkdir -p /root/printer_data/misc
    mkdir -p /root/printer_data/tmp

    # Init /root/printer_data
    ln -fns /data /root/printer_data/gcodes
    ln -fns "$LOG_DIR" /root/printer_data/logs
    ln -fns /opt/config/ /root/printer_data/config
    ln -fns /opt/config/mod_data/database /root/printer_data/database
    ln -fns /opt/config/mod/.bin/exec /root/printer_data/bin
    ln -fns /opt/config/mod/.py /root/printer_data/py
    ln -fns /opt/config/mod/.shell /root/printer_data/scripts

    ln -fns /opt/config/mod/moonraker.conf /root/printer_data/config/moonraker.conf
    ln -fns /opt/config/mod/.root/moonraker.asvc /root/printer_data/moonraker.asvc

    sync

    mount --bind /root/printer_data "$MOD"/root/printer_data

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
        --params /opt/config/mod/.cfg/init.move.cfg; then
        # TODO: Merge with defaults?
        DATA_MOVE_CFG=$TMP_CFG_PATH
    else
        DATA_MOVE_CFG=/opt/config/mod/.cfg/data.init.move.cfg
    fi

    # Create the batch file
    echo "[" > $BATCH_FILE

    # 2. Move params from printer.base.cfg to printer.cfg
    echo "
    {
        \"mode\": \"restore\",
        \"config\": \"/opt/config/printer.cfg\",
        \"data\": \"$DATA_MOVE_CFG\",
        \"params\": \"/opt/config/mod/.cfg/init.move.cfg\",
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
        \"params\": \"/opt/config/mod/.cfg/init.cfg\",
        \"data\": \"/opt/config/mod/.cfg/data.init.cfg\",
        \"avoid_writes\": true
    }," >> $BATCH_FILE

    # 5. Init printer.base.cfg configuration
    echo "
    {
        \"mode\": \"restore\",
        \"config\": \"/opt/config/printer.base.cfg\",
        \"params\": \"/opt/config/mod/.cfg/init.base.cfg\",
        \"data\": \"/opt/config/mod/.cfg/data.init.base.cfg\",
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
