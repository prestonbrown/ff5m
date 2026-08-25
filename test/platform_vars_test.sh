#!/bin/sh
# Platform descriptor value-preservation test.
#
# Every variable in the platform descriptor must expand to the exact literal it
# replaced, so that abstracting them changes no behaviour on AD5M. The AD5X
# block is pinned to the values measured on the rig.
#
# platform.sh selects its block from `uname -m`; this test runs off-printer on
# whatever the dev/CI host is, so it shadows `uname` with a shell function to
# force each architecture. A function named `uname` is found ahead of the real
# binary, including inside the `$(uname -m)` command substitution.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

DESCRIPTOR="$REPO_DIR/.shell/platform.sh"

# --- AD5M (armv7l) ----------------------------------------------------------
uname() { echo armv7l; }
# shellcheck disable=SC1090
. "$DESCRIPTOR"

assert_eq "ad5m PLATFORM"       "ad5m"                       "$PLATFORM"
assert_eq "ad5m PLATFORM_NAME"  "AD5M"                       "$PLATFORM_NAME"
assert_eq "ad5m ROOT_PART"      "/dev/mmcblk0p6"             "$ROOT_PART"
assert_eq "ad5m DATA_PART"      "/dev/mmcblk0p7"             "$DATA_PART"
assert_eq "ad5m DATA_MNT"       "/data"                      "$DATA_MNT"
assert_eq "ad5m LOG_DIR"        "/data/logFiles"             "$LOG_DIR"
assert_eq "ad5m KLIPPER_DIR"    "/opt/klipper"               "$KLIPPER_DIR"
assert_eq "ad5m MOD_ROOT"       "/opt/config/mod"            "$MOD_ROOT"
assert_eq "ad5m MOD"            "/data/.mod/.forge-x"        "$MOD"
assert_eq "ad5m STOCK_UI_PROCS" "ffstartup-arm firmwareExe"  "$STOCK_UI_PROCS"

# --- AD5X (mips) ------------------------------------------------------------
uname() { echo mips; }
# shellcheck disable=SC1090
. "$DESCRIPTOR"

assert_eq "ad5x PLATFORM"       "ad5x"                       "$PLATFORM"
assert_eq "ad5x PLATFORM_NAME"  "AD5X"                       "$PLATFORM_NAME"
assert_eq "ad5x ROOT_PART"      "/dev/mmcblk0p6"             "$ROOT_PART"
assert_eq "ad5x DATA_PART"      "/dev/mmcblk0p7"             "$DATA_PART"
assert_eq "ad5x DATA_MNT"       "/usr/data"                  "$DATA_MNT"
assert_eq "ad5x LOG_DIR"        "/usr/data/logs"             "$LOG_DIR"
assert_eq "ad5x KLIPPER_DIR"    "/usr/prog/klipper"          "$KLIPPER_DIR"
assert_eq "ad5x MOD_ROOT"       "/usr/data/config/mod"       "$MOD_ROOT"
assert_eq "ad5x MOD"            "/usr/data/.mod/.forge-x"    "$MOD"
assert_eq "ad5x STOCK_UI_PROCS" "firmwareExe"                "$STOCK_UI_PROCS"

# mipsel must select the same block as mips (the mips* glob).
uname() { echo mipsel; }
# shellcheck disable=SC1090
. "$DESCRIPTOR"
assert_eq "mipsel -> ad5x"      "ad5x"                       "$PLATFORM"

unset -f uname
finish
