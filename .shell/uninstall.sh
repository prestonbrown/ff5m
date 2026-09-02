#!/bin/bash

## Mod's uninstall script
##
## Copyright (C) 2025-2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

source /opt/config/mod/.shell/common.sh

## Klipper overlay teardown comes from klipper_overlay.sh, which owns the rule.
## This file used to carry its own copy, and the two had drifted: this one walked
## the SOURCE tree and so only removed plugins whose source file still existed,
## and only restored patches that had a .bak. Anything whose source had since been
## deleted was left behind as a dangling symlink into a tree that is about to go
## away - the exact leftover that stops stock klippy starting. The shared
## klipper_overlay_remove_all() walks the TARGET tree instead, so it cannot miss
## those, and it is covered by tests.
## shellcheck source=/dev/null
source /opt/config/mod/.shell/klipper_overlay.sh

revert_klipper_patches_and_tuning() {
    revert_klipper_patches || return 1

    # Klipper tunning - not the overlay's business, so it stays here.
    "$CMDS"/ztune_klipper.sh 0
}

fail() {
    if [ -n "$1" ]; then echo "$1"; fi

    sync
    sleep 1

    echo "@@ Failed to remove mod. Reboot the printer."
    sync

    sleep 300
    reboot
}

uninstall() {
    if ! mount | grep -q "$MOD/sys"; then
        echo "// Init chroot..."
        init_chroot
        
        mount --bind /opt/config "$MOD"/opt/config
    fi
    
    echo "// Restore config..."
    
    chroot "$MOD" /bin/python3 /opt/config/mod/.py/cfg_backup.py \
        --mode restore \
        --config /opt/config/printer.cfg \
        --no_data \
        --params /opt/config/mod/.cfg/restore.cfg \
        || fail "@@ Failed to restore printer.cfg"
    
    chroot "$MOD" /bin/python3 /opt/config/mod/.py/cfg_backup.py \
        --mode restore \
        --config /opt/config/printer.base.cfg \
        --params /opt/config/mod/.cfg/restore.base.cfg \
        --data /opt/config/mod/.cfg/data.restore.base.cfg \
        || fail "@@ Failed to restore printer.base.cfg"
    
    echo "127.0.0.1       localhost" > /etc/hosts
    echo "127.0.1.1       kunos" >> /etc/hosts
    
    echo "// Restore klipper..."
    
    revert_klipper_patches_and_tuning
    
    echo "// Remove mod..."
    
    # Make sure to umount all mounted files
    # In case of accidentally run this script after init
    echo "// Unmount paths..."
    
    mount | grep "$DATA_MNT/.mod" | awk '{print $3}' | xargs -n1 -I {} umount -lf "{}"
    mount | grep " /root/printer_data" | awk '{print $3}' | xargs -n1 -I {} umount -lf "{}"
    umount -lf /root/.oh-my-zsh &> /dev/null

    if mount | grep -q "$DATA_MNT/.mod" || lsof | grep -q "$DATA_MNT/.mod"; then
        echo "@@ Found running mod services."
        fail
    fi
    
    echo "// Removing services..."
    
    rm -f /etc/init.d/S00fix
    rm -f /etc/init.d/S00init
    rm -f /etc/init.d/S55boot
    rm -f /etc/init.d/S99root
    rm -f /etc/init.d/S99moon
    rm -f /etc/init.d/S98camera
    rm -f /etc/init.d/S98zssh
    rm -f /etc/init.d/K99moon
    rm -f /etc/init.d/K99root
    
    echo "// Removing zsh..."
    rm -rf /root/.profile
    rm -rf /root/.zshrc
    echo "" > /etc/motd
    
    echo "// Removing entware..."
    rm -rf /opt/bin
    rm -rf /opt/etc
    rm -rf /opt/home
    rm -rf /opt/lib
    rm -rf /opt/libexec
    rm -rf /opt/root
    rm -rf /opt/sbin
    rm -rf /opt/share
    rm -rf /opt/tmp
    rm -rf /opt/usr
    rm -rf /opt/var
    
    if [ "$1" != "--soft" ]; then
        echo "// Hard remove step..."
        rm -rf /opt/config/mod_data
        rm -rf /opt/.netd-private
        
        echo "// Removing root access..."
        rm -rf /etc/init.d/S50sshd /etc/init.d/S55date /bin/dropbearmulti /bin/dropbear /bin/dropbearkey /bin/scp /etc/dropbear /etc/init.d/S60dropbear
        
        echo "// Removing Beep util..."
        rm -f /usr/bin/audio /usr/lib/python3.7/site-packages/audio.py /usr/bin/audio_midi.sh "$KLIPPER_DIR/klippy/extras/gcode_shell_command.py"
        rm -rf /usr/lib/python3.7/site-packages/mido/
    else
        echo "// Preserve root..."
        cp /opt/config/mod/.shell/S60dropbear /etc/init.d/S60dropbear
    fi

    echo "// Removing mod files..."
    
    rm -rf /opt/config/mod/
    rm -rf /root/printer_data
    rm -rf "$DATA_MNT/.mod"
    
    echo "// Done!"
    
    sync
    sleep 1

    echo "// Reboot the printer"
    sync
    
    sleep 300
    reboot
}

xzcat /opt/config/mod/uninstall.img.xz > /dev/fb0

mv "$LOG_DIR/uninstall.1.log" "$LOG_DIR/uninstall.log.2" &> /dev/null
mv "$LOG_DIR/uninstall.log" "$LOG_DIR/uninstall.log.1"   &> /dev/null

uninstall "$1" 2>&1 | logged "$LOG_DIR/uninstall.log" --send-to-screen --screen-no-followup --screen-queue 10
