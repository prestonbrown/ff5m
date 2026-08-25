#!/bin/sh
# Any script using a platform descriptor variable must source the descriptor.
#
# An unset shell variable expands to the empty string rather than failing, so a
# missing source turns "$LOG_DIR/boot.log" into "/boot.log" with no error at all.
# This gate is the reason the descriptor refactor is safe to do mechanically.
#
# Sourcing platform.sh directly OR sourcing common.sh (which sources it) both
# count. #!/bin/sh scripts must use the former: common.sh is bash-only.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

cd "$REPO_DIR" || exit 1

DESCRIPTOR_VARS="PLATFORM_NAME ROOT_PART DATA_PART DATA_MNT LOG_DIR KLIPPER_DIR STOCK_UI_PROCS PLATFORM"

scanned=0
for f in $(git ls-files '.shell/*' '.root/*'); do
    [ -f "$f" ] || continue
    # The descriptor defines them; common.sh sources it. Neither needs checking.
    [ "$f" = ".shell/platform.sh" ] && continue
    [ "$f" = ".shell/common.sh" ] && continue

    uses=""
    for v in $DESCRIPTOR_VARS; do
        # -E so the alternation is portable; \b-free because BusyBox grep lacks it.
        # PLATFORM is checked last and only counts if PLATFORM_NAME did not match,
        # since "$PLATFORM" is a substring of "$PLATFORM_NAME".
        if [ "$v" = "PLATFORM" ]; then
            case "$uses" in *PLATFORM_NAME*) continue ;; esac
        fi
        if grep -qE "[$]$v([^A-Za-z0-9_]|$)|[$][{]$v[}]" "$f" 2>/dev/null; then
            uses="$uses $v"
        fi
    done

    [ -z "$uses" ] && continue
    scanned=$((scanned + 1))

    # Must be an actual source line, not a passing mention in a comment.
    if grep -qE '^[[:space:]]*([.]|source)[[:space:]].*(platform|common)[.]sh' "$f"; then
        _t_pass "sources descriptor: $f (uses$uses)"
    else
        _t_fail "sources descriptor: $f" \
                "uses$uses but never sources platform.sh or common.sh; those expand to empty"
    fi
done

echo "     ($scanned file(s) reference the descriptor)"
finish
