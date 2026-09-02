#!/bin/sh
# Re-derive the AD5X Klipper patch set, or verify the committed one still matches.
#
## Copyright (C) 2026, Preston Brown <https://github.com/prestonbrown>
##
## This file may be distributed under the terms of the GNU GPLv3 license

set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TOOL="$ROOT/tools/klipper-merge"
BASE="$TOOL/stock/ad5m"     # what Forge-X's AD5M patches were written against
OURS="$TOOL/stock/ad5x"     # AD5X stock, which we must not regress
THEIRS="$ROOT/.py/klipper/patches"      # Forge-X's AD5M patch set
DEST="$ROOT/.py/klipper/patches.ad5x"   # AD5X overrides

# Files where AD5M and AD5X stock are byte-identical need no override: Forge-X's
# own patch applies verbatim. Everything else is merged three ways.
MERGED="configfile.py gcode.py extras/buttons.py extras/resonance_tester.py
        extras/shaper_calibrate.py extras/statistics.py extras/virtual_sdcard.py"
SHARED="extras/gcode_move.py extras/led.py extras/temperature_sensor.py
        extras/gcode_shell_command.py"

WORK=${TMPDIR:-/tmp}/klipper-merge.$$
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/extras"

mode=${1:-verify}
rc=0

note() { printf '%s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; rc=1; }

# A file whose stock is shared must stay shared: if AD5M and AD5X stock ever
# diverge, the AD5M patch silently stops being right for AD5X.
for f in $SHARED; do
    [ "$f" = "extras/gcode_shell_command.py" ] && continue  # no stock base exists
    if cmp -s "$BASE/$f" "$OURS/$f"; then
        note "shared    $f"
    else
        fail "$f: stock diverged between AD5M and AD5X - it now needs an override"
    fi
    if [ -e "$DEST/$f" ]; then
        fail "$f: has an AD5X override but its stock is shared - remove the override"
    fi
done

for f in $MERGED; do
    cp "$OURS/$f" "$WORK/$f"
    git merge-file -L AD5X-stock -L AD5M-stock -L Forge-X \
        "$WORK/$f" "$BASE/$f" "$THEIRS/$f" >/dev/null 2>&1 || true
    n=$(grep -c '^<<<<<<<' "$WORK/$f" || true)

    if [ "$n" -eq 0 ]; then
        # Fully mechanical: the committed file must equal what we just produced.
        if [ "$mode" = update ]; then
            mkdir -p "$DEST/$(dirname "$f")"
            cp "$WORK/$f" "$DEST/$f"
            note "generated $f"
        elif cmp -s "$WORK/$f" "$DEST/$f"; then
            note "clean     $f"
        else
            fail "$f: committed file differs from the mechanical merge (run '$0 update')"
        fi
        continue
    fi

    # Hand-resolved: we cannot regenerate it, but we can detect that the inputs
    # moved underneath the resolution. Fingerprint the WHOLE merge output, not
    # just the conflict regions -- statistics.py lost Forge-X's "disabled" guard
    # in a region that merged clean, because AD5X had reordered the two lines
    # around it. A conflict-only fingerprint would not have noticed.
    sum=$(sha256sum < "$WORK/$f" | cut -d' ' -f1)
    want=$(awk -v k="$f" '$1 == k { print $2 }' "$TOOL/conflicts.sha256" 2>/dev/null || true)

    if [ "$mode" = update ]; then
        note "conflicts $f ($n regions, $sum)"
        printf '%s %s\n' "$f" "$sum" >> "$WORK/conflicts.new"
    elif [ -z "$want" ]; then
        fail "$f: $n conflict regions but no recorded fingerprint"
    elif [ "$want" = "$sum" ]; then
        note "resolved  $f ($n regions, fingerprint unchanged)"
    else
        fail "$f: inputs moved under the resolution ($want -> $sum) - re-resolve by hand"
    fi
done

# Files AD5X excludes: base patches that are NOT overlaid on AD5X because its
# stock Klipper is newer than the AD5M base they were written against, so the
# overlay would regress it. The overlay reads patches.ad5x/.exclude; here we
# only confirm that list stays coherent - every entry is a real base patch and
# is not also given an override (the two are mutually exclusive).
EXCLUDE_FILE="$DEST/.exclude"
if [ -f "$EXCLUDE_FILE" ]; then
    while IFS= read -r line; do
        case "$line" in ''|\#*) continue ;; esac
        if [ ! -e "$THEIRS/$line" ]; then
            fail "excluded $line: no such base patch in .py/klipper/patches"
        elif [ -e "$DEST/$line" ]; then
            fail "excluded $line: also has an override - exclude and override are mutually exclusive"
        else
            note "excluded  $line"
        fi
    done < "$EXCLUDE_FILE"
fi

if [ "$mode" = update ] && [ -f "$WORK/conflicts.new" ]; then
    sort "$WORK/conflicts.new" > "$TOOL/conflicts.sha256"
    note "wrote $TOOL/conflicts.sha256"
fi

exit $rc
