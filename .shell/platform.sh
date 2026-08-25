#!/bin/sh

## Platform descriptor
##
## Every board-specific value used anywhere in the mod is named here. Scripts
## consume these variables rather than hardcoding literals, so that supporting
## a second board is a matter of defining a second descriptor rather than
## editing every script.
##
## AD5M is currently the only supported platform, so each value below is the
## literal it replaced. test/platform_vars_test.sh enforces that.
##
## POSIX-clean by requirement: this file is sourced by #!/bin/sh scripts that
## run under BusyBox ash on the printer. Plain assignments only - no arrays,
## no declare, no [[, no local, no command substitution.
##
## Every value here is consumed by other files, which shellcheck cannot see
## from inside this one, so it reports all of them as unused.
# shellcheck disable=SC2034

PLATFORM=ad5m
PLATFORM_NAME=AD5M

ROOT_PART=/dev/mmcblk0p6
DATA_PART=/dev/mmcblk0p7
DATA_MNT=/data

LOG_DIR=$DATA_MNT/logFiles
KLIPPER_DIR=/opt/klipper

## Stock FlashForge UI processes that must be stopped for an alternative
## screen to own the framebuffer. Space-separated, iterated with `for`.
STOCK_UI_PROCS="ffstartup-arm firmwareExe"
