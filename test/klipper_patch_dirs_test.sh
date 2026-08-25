#!/bin/sh
# Klipper patch overlay resolution test.
#
# A platform whose stock Klipper differs from the one Forge-X's patches were
# written against gets an overlay directory applied on top of the shared set.
# Two things must hold: AD5M's behaviour is unchanged (it has no overlay, so it
# must resolve to exactly one directory), and a platform that has one must get
# the overlay LAST, because ordering is what makes its files win.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

# common.sh is bash-only, so ask bash for the result rather than sourcing it.
dirs_for() {
    bash -c '
        . "$1" >/dev/null 2>&1
        PLATFORM=$2
        KLIPPER_SRC=$3
        klipper_patch_dirs | sed "s|^$KLIPPER_SRC/||" | tr "\n" " "
    ' _ "$REPO_DIR/.shell/common.sh" "$1" "$REPO_DIR/.py/klipper"
}

if ! command -v bash >/dev/null 2>&1; then
    fail "klipper_patch_dirs" "no bash available, cannot source common.sh"
    finish
fi

assert_eq "ad5m resolves to the shared set only" \
    "patches " "$(dirs_for ad5m)"

assert_eq "ad5x appends its overlay, and appends it last" \
    "patches patches.ad5x " "$(dirs_for ad5x)"

# A platform naming a directory that does not exist must not produce a path the
# install would then walk and find nothing in.
assert_eq "an absent overlay is omitted, not emitted empty" \
    "patches " "$(dirs_for nosuchboard)"

finish
