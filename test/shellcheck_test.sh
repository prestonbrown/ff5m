#!/bin/sh
# Static analysis gate: no shellcheck findings beyond the recorded baseline.
#
# Forge-X has never run shellcheck, so there is a large body of pre-existing
# findings. Failing on all of them would make this unmergeable, so the gate
# compares against a recorded baseline and fails only on findings that are new.
#
# The baseline stores file:code pairs, not line numbers, so that unrelated
# edits which shift lines do not produce spurious failures.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

cd "$REPO_DIR" || exit 1

# The shellcheck binary is a dev and CI dependency, never needed on the printer.
# On a machine without it the suite must still be usable, so skip rather than fail.
if ! command -v shellcheck >/dev/null 2>&1; then
    echo "ok     shellcheck not installed, skipping"
    finish
fi

BASELINE="$SCRIPT_DIR/shellcheck_baseline.txt"
assert_file "baseline exists" "$BASELINE"
[ -f "$BASELINE" ] || finish

FILES=$(mktemp)
CURRENT=$(mktemp)
BASE=$(mktemp)

# The file list is built in its own loop rather than piped straight into
# the checker, because a counter incremented inside a pipeline lands in a
# subshell and the count would be lost.
scanned=0
for f in $(git ls-files '.shell/*' '.root/*'); do
    [ -f "$f" ] || continue
    head -1 "$f" 2>/dev/null | grep -q '^#!' || continue
    scanned=$((scanned + 1))
    echo "$f"
done > "$FILES"

while IFS= read -r f; do
    shellcheck -f gcc "$f" 2>/dev/null
done < "$FILES" \
    | sed -E 's/^([^:]+):[0-9]+:[0-9]+: [a-z]+: .*\[(SC[0-9]+)\]$/\1:\2/' \
    | LC_ALL=C sort -u > "$CURRENT"

# Strip the baseline's comment header and re-sort it here rather than trusting
# the file's own order: a hand-edited baseline that is out of order makes comm
# report nonsense instead of erroring.
grep -v '^#' "$BASELINE" | grep -v '^[[:space:]]*$' | LC_ALL=C sort -u > "$BASE"

# comm requires both inputs collated identically. Without LC_ALL=C the baseline
# generated on a dev box (en_US.UTF-8) and the list generated in CI (C) sort
# differently, and comm silently reports phantom additions.
NEW=$(LC_ALL=C comm -13 "$BASE" "$CURRENT")

baseline_count=$(wc -l < "$BASE" | tr -d '[:space:]')
current_count=$(wc -l < "$CURRENT" | tr -d '[:space:]')
rm -f "$FILES" "$CURRENT" "$BASE"

# A wrong cwd, a missing git, or a pathspec typo would leave nothing to check
# and an empty diff would read as success.
assert_ne "scripts were actually scanned" "0" "$scanned"

# Likewise if shellcheck ran but produced nothing at all: with a non-empty
# baseline that means the scan broke, not that the tree got clean. A genuine
# tree-wide cleanup regenerates the baseline and clears this too.
if [ "$baseline_count" -gt 0 ] && [ "$current_count" -eq 0 ]; then
    _t_fail "shellcheck produced findings" \
        "baseline records $baseline_count finding(s) but this run produced none; the scan is broken"
else
    _t_pass "shellcheck produced findings"
fi

if [ -z "$NEW" ]; then
    _t_pass "no new shellcheck findings"
else
    _t_fail "no new shellcheck findings" "$(echo "$NEW" | head -10)"
    echo "       (regenerate the baseline only if these are intentional)"
fi

finish
