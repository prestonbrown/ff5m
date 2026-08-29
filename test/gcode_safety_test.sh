#!/bin/sh
# Static analysis gate: no gcode command handler may take the printer down.
#
# Klipper turns any exception that is not a gcode error into "Internal error on
# command", which puts klippy into SHUTDOWN and takes the MCUs with it. That has
# happened twice on the AD5X - a refused IFS opcode mid-load, and a TONE that
# hit a PWM device this board does not have, which killed a filament change that
# had already succeeded.
#
# A handler must contain a try, or carry a `## GCODE_SAFE: <reason>` comment.
# Handlers in upstream files we have not touched are recorded in
# tools/lint/gcode_safety_baseline.txt; that list may shrink and never grow.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

cd "$REPO_DIR" || exit 1

# Python 3 is a dev dependency here, not something the printer needs for this.
if ! command -v python3 >/dev/null 2>&1; then
    echo "ok     python3 not installed, skipping"
    finish
fi

CHECKER="tools/lint/check_gcode_safety.py"
assert_file "checker exists" "$CHECKER"
assert_file "baseline exists" "tools/lint/gcode_safety_baseline.txt"

OUT=$(mktemp)
python3 "$CHECKER" > "$OUT" 2>&1
status=$?

if [ "$status" -ne 0 ]; then
    echo "       ---- checker output ----"
    sed 's/^/       /' "$OUT"
fi
assert_eq "no unguarded gcode command handlers" "0" "$status"
echo "       $(tail -1 "$OUT")"

rm -f "$OUT"
finish
