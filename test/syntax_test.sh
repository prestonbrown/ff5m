#!/bin/sh
# Every shipped script must parse under the interpreter its shebang declares.
#
# This is the gate that catches a bashism landing in a #!/bin/sh file. Such a
# file parses fine on a dev machine, where /bin/sh is frequently bash, and then
# fails on the printer, where it is BusyBox ash.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

cd "$REPO_DIR" || exit 1

# Scripts that already fail on upstream main. Fixing them is a separate
# concern from adding this gate; the gate exists to stop NEW breakage.
KNOWN_BAD=".root/stop.sh"

is_known_bad() {
    for k in $KNOWN_BAD; do
        [ "$1" = "$k" ] && return 0
    done
    return 1
}

scanned=0
for f in $(git ls-files '.shell/*' '.root/*' '.py/*'); do
    [ -f "$f" ] || continue
    head -1 "$f" 2>/dev/null | grep -q '^#!' || continue
    is_known_bad "$f" && continue
    scanned=$((scanned + 1))

    if head -1 "$f" | grep -q bash; then
        if bash -n "$f" 2>/dev/null; then
            _t_pass "parses (bash): $f"
        else
            _t_fail "parses (bash): $f" "$(bash -n "$f" 2>&1 | head -3)"
        fi
    elif head -1 "$f" | grep -qE 'sh$|sh '; then
        if sh -n "$f" 2>/dev/null; then
            _t_pass "parses (sh): $f"
        else
            _t_fail "parses (sh): $f" "$(sh -n "$f" 2>&1 | head -3)"
        fi
    fi
done

# A wrong cwd, a missing git, or a pathspec typo would make the loop above run
# zero times and this suite would report "0 assertions, 0 failed" as success.
assert_ne "scripts were actually scanned" "0" "$scanned"

finish
