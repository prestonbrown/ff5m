#!/bin/sh
# Platform descriptor value-preservation test.
#
# Every variable in the platform descriptor must expand to the exact literal it
# replaced, so that abstracting them changes no behaviour on AD5M.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

# Source the descriptor directly. NOT common.sh: that file is bash-only
# (arrays at line 117) and this test runs under sh via test/run.sh.
# shellcheck disable=SC1090,SC1091
. "$REPO_DIR/.shell/platform.sh"

assert_eq "PLATFORM"       "ad5m"                      "$PLATFORM"
assert_eq "PLATFORM_NAME"  "AD5M"                      "$PLATFORM_NAME"
assert_eq "DATA_PART"      "/dev/mmcblk0p7"            "$DATA_PART"
assert_eq "ROOT_PART"      "/dev/mmcblk0p6"            "$ROOT_PART"
assert_eq "DATA_MNT"       "/data"                     "$DATA_MNT"
assert_eq "LOG_DIR"        "/data/logFiles"            "$LOG_DIR"
assert_eq "KLIPPER_DIR"    "/opt/klipper"              "$KLIPPER_DIR"
assert_eq "STOCK_UI_PROCS" "ffstartup-arm firmwareExe" "$STOCK_UI_PROCS"

# MOD lives in common.sh, not the descriptor, but this task rewrites it in terms
# of DATA_MNT and it is the only value that rewrite can break. common.sh is
# bash-only, so ask bash for the result rather than sourcing it under sh.
if command -v bash >/dev/null 2>&1; then
    mod_got=$(bash -c '. "$1" >/dev/null 2>&1; printf %s "$MOD"' _ "$REPO_DIR/.shell/common.sh")
    assert_eq "MOD (via common.sh)" "/data/.mod/.forge-x" "$mod_got"
else
    fail "MOD (via common.sh)" "no bash available, cannot source common.sh"
fi

finish
