#!/bin/sh

## Platform descriptor
##
## Every board-specific value used anywhere in the mod is named here. Scripts
## consume these variables rather than hardcoding literals, so that supporting
## a second board is a matter of defining a second descriptor rather than
## editing every script.
##
## The block is selected by machine architecture: armv7l is the AD5M, mips is
## the AD5X. test/platform_vars_test.sh pins every value of both blocks; the
## AD5M values are the literals they replaced and must not drift.
##
## POSIX-clean by requirement: this file is sourced by #!/bin/sh scripts that
## run under BusyBox ash on the printer. Plain assignments only - no arrays,
## no declare, no [[, no local. The ONE exception is the single `uname -m`
## command substitution below that selects the block; nothing else here runs a
## command.
##
## Every value here is consumed by other files, which shellcheck cannot see
## from inside this one, so it reports all of them as unused.
# shellcheck disable=SC2034

# armv7l -> AD5M, mips/mipsel -> AD5X. A test may shadow `uname` to force either.
case "$(uname -m)" in
    mips*)
        PLATFORM=ad5x
        PLATFORM_NAME=AD5X

        ROOT_PART=/dev/mmcblk0p6
        DATA_PART=/dev/mmcblk0p7
        DATA_MNT=/usr/data

        LOG_DIR=/usr/data/logs
        KLIPPER_DIR=/usr/prog/klipper

        ## Mod source tree (the checked-out .shell/.py/.root/... payload).
        MOD_ROOT=/usr/data/config/mod

        ## Stock FlashForge UI processes that must be stopped for an alternative
        ## screen to own the framebuffer. Space-separated, iterated with `for`.
        STOCK_UI_PROCS="firmwareExe"

        ## Dynamic loader inside $MOD, used to run the rootfs bash on a host
        ## that ships none: AD5X stock is BusyBox with a read-only squashfs
        ## /bin, so #!/bin/bash scripts cannot start directly. Consumed by the
        ## bash-trampoline preludes (see .shell/ad5x_bootstrap.sh).
        ROOTFS_LD=ld-linux-mipsn8.so.1
        ;;
    *)
        ## armv7l, and the default for anything unrecognised: AD5M. Keeping AD5M
        ## as the default preserves this file's original behaviour (it was a flat
        ## AD5M literal file with no detection at all).
        PLATFORM=ad5m
        PLATFORM_NAME=AD5M

        ROOT_PART=/dev/mmcblk0p6
        DATA_PART=/dev/mmcblk0p7
        DATA_MNT=/data

        LOG_DIR=$DATA_MNT/logFiles
        KLIPPER_DIR=/opt/klipper

        ## Mod source tree (the checked-out .shell/.py/.root/... payload).
        MOD_ROOT=/opt/config/mod

        ## Stock FlashForge UI processes that must be stopped for an alternative
        ## screen to own the framebuffer. Space-separated, iterated with `for`.
        STOCK_UI_PROCS="ffstartup-arm firmwareExe"

        ## Dynamic loader inside $MOD (see the AD5X block). Unused on the AD5M,
        ## whose stock host already provides /bin/bash, but defined for symmetry
        ## so every descriptor consumer resolves on both boards.
        ROOTFS_LD=ld-linux-armhf.so.3
        ;;
esac

## Chroot rootfs (Forge-X's second Buildroot root, entered with chroot). A
## single derivation off DATA_MNT rather than a per-board literal: /data on the
## AD5M and /usr/data on the AD5X both yield the right path. Defined here, once,
## so that every descriptor consumer - including #!/bin/sh scripts that cannot
## source the bash-only common.sh - can read it.
MOD=$DATA_MNT/.mod/.forge-x

## The mod's own bash and its dynamic loader, assembled once from MOD and
## ROOTFS_LD. The bash-trampoline preludes exec through these rather than
## naming MOD themselves, so a #!/bin/bash mod script can run on a host with no
## /bin/bash (AD5X stock) by re-execing under the rootfs bash. On the AD5M these
## resolve to an arm rootfs bash that is never invoked - that host has bash.
FORGEX_BASH_LD=$MOD/lib/$ROOTFS_LD
FORGEX_BASH_LIBPATH=$MOD/lib:$MOD/usr/lib
FORGEX_BASH_BIN=$MOD/bin/bash
