#!/bin/sh
# Display mode configs must be mutually exclusive.
#
# Each .cfg/init.display.<mode>.cfg includes its own config/<mode>.cfg and
# removes every other mode's with a -[include ...] line. Adding a mode without
# teaching the others to exclude it leaves two display configs active at once,
# and both define _PRINT_STATUS and reset_screen.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

cd "$REPO_DIR" || exit 1

# Derive the mode list from the files themselves so a new mode is covered
# automatically rather than needing this test updated.
MODES=""
for f in .cfg/init.display.*.cfg; do
    [ -f "$f" ] || continue
    m=$(basename "$f" .cfg)
    m=${m#init.display.}
    MODES="$MODES $m"
done

assert_ne "at least one display mode found" "" "$MODES"

for mode in $MODES; do
    f=".cfg/init.display.$mode.cfg"

    # It must include its own config.
    if grep -q "^\[include \./mod/config/$mode\.cfg\]" "$f"; then
        _t_pass "$mode includes its own config"
    else
        _t_fail "$mode includes its own config" "no [include ./mod/config/$mode.cfg] in $f"
    fi

    # It must exclude every other mode's config.
    for other in $MODES; do
        [ "$other" = "$mode" ] && continue
        if grep -q -- "^-\[include \./mod/config/$other\.cfg\]" "$f"; then
            _t_pass "$mode excludes $other"
        else
            _t_fail "$mode excludes $other" "missing -[include ./mod/config/$other.cfg] in $f"
        fi
    done
done

finish
