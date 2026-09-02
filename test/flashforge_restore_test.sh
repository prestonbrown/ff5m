#!/bin/sh
# AD5X rescue stick: does it actually put a broken printer back to stock?
#
# Off-rig. Builds a fake root that reproduces the state a failed bring-up leaves
# behind - klippy symlinked into the mod tree, a mod-flavoured printer.cfg, and
# the hook injected into app_startup.sh - then runs the real script against it
# through its FF_* seams and checks the printer would boot stock afterwards.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

RESTORE="$REPO_DIR/tools/release/ad5x/flashforge_restore.sh"
assert_file "restore script exists" "$RESTORE"

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

DATA="$ROOT/usr/data"
CFG="$DATA/config"
MODR="$CFG/mod"
KLIP="$ROOT/usr/prog/klipper"
APP="$ROOT/usr/prog/app_startup.sh"

mkdir -p "$CFG" "$MODR/.shell" "$CFG/mod_data/log" "$KLIP/klippy/extras" \
         "$ROOT/usr/prog" "$ROOT/stick"

# A stock file that the overlay replaced, with its .bak preserved.
printf 'STOCK_MCU = True\n' > "$KLIP/klippy/mcu.py.bak"
ln -s "$MODR/.py/klipper/patches/mcu.py" "$KLIP/klippy/mcu.py"
# A mod-only addition: no backup, so it must be deleted outright.
ln -s "$MODR/.py/klipper/plugins/forgex_only.py" "$KLIP/klippy/extras/forgex_only.py"
# A foreign symlink that is none of our business.
printf 'EXTERNAL\n' > "$ROOT/elsewhere.py"
ln -s "$ROOT/elsewhere.py" "$KLIP/klippy/extras/external.py"

# fix_config left a mod-flavoured printer.cfg; the display switch left a clean one.
printf '[include /opt/config/mod/.cfg/init.display.headless.cfg]\nMOD = 1\n' > "$CFG/printer.cfg"
printf '[extruder]\nSTOCK = 1\n' > "$CFG/printer.cfg.feather-before-headless.bak"
# printer.base.cfg is untouched and stock-loadable: it must be LEFT ALONE.
printf '[mcu]\nserial: /dev/ttyS0\n' > "$CFG/printer.base.cfg"
cp "$CFG/printer.base.cfg" "$ROOT/base-before"

# The bring-up log the stick exists to retrieve.
printf '// [ad5x] Launching klipper...\n@@ [ad5x] LAST LINE BEFORE THE HANG\n' \
    > "$CFG/mod_data/log/ad5x_bootstrap.log"

# app_startup.sh with the hook injected, plus the pristine .orig.
printf '#!/bin/sh\necho stock\n' > "$APP.orig"
{ cat "$APP.orig"; printf '[ -x %s/.shell/ad5x_bootstrap.sh ] && ...   # forge-x\n' "$MODR"; } > "$APP"
cp "$APP.orig" "$ROOT/app-pristine"

printf '#!/bin/sh\n' > "$MODR/.shell/ad5x_bootstrap.sh"
chmod +x "$MODR/.shell/ad5x_bootstrap.sh"
: > "$MODR/BOOT_FLAG_FAILURE"

# Ship the factory fallback the way the stick does.
mkdir -p "$ROOT/stick/cfg"
printf '[include printer.base.cfg]\nFACTORY = 1\n' > "$ROOT/stick/cfg/printer.cfg"
cp "$RESTORE" "$ROOT/stick/flashforge_init.sh"
chmod +x "$ROOT/stick/flashforge_init.sh"

OUT=$(FF_DATA_MNT="$DATA" FF_KLIPPER_DIR="$KLIP" FF_APP_STARTUP="$APP" \
      FF_FB="$ROOT/fb" sh "$ROOT/stick/flashforge_init.sh" 2>&1)
RC=$?

assert_eq "restore exits 0" "0" "$RC"

# --- klipper overlay -------------------------------------------------------
remaining=$(find "$KLIP" -type l 2>/dev/null | while read -r l; do
    case "$(readlink "$l")" in "$MODR"/*) echo x ;; esac
done | wc -l)
assert_eq "every overlay link is gone" "0" "$remaining"
assert_eq "backed-up stock file is restored" "STOCK_MCU = True" "$(cat "$KLIP/klippy/mcu.py")"
if [ -e "$KLIP/klippy/extras/forgex_only.py" ]; then
    _t_fail "backup-less mod link is removed" "still present"
else
    _t_pass "backup-less mod link is removed"
fi
if [ -L "$KLIP/klippy/extras/external.py" ]; then
    _t_pass "foreign symlink is left alone"
else
    _t_fail "foreign symlink is left alone" "it was removed"
fi

# --- config ----------------------------------------------------------------
assert_contains "mod-flavoured printer.cfg is replaced" "$(cat "$CFG/printer.cfg")" "STOCK = 1"
assert_file "the mod's version is kept for inspection" "$CFG/printer.cfg.forgex-was"
assert_eq "stock-loadable printer.base.cfg is NOT touched" \
    "$(cat "$ROOT/base-before")" "$(cat "$CFG/printer.base.cfg")"

# --- app_startup -----------------------------------------------------------
assert_eq "app_startup.sh is restored byte-for-byte" \
    "$(cat "$ROOT/app-pristine")" "$(cat "$APP")"
case "$(cat "$APP")" in
    *forge-x*) _t_fail "the hook is gone from app_startup.sh" "still present" ;;
    *)         _t_pass "the hook is gone from app_startup.sh" ;;
esac

# --- disarm ----------------------------------------------------------------
if [ -x "$MODR/.shell/ad5x_bootstrap.sh" ]; then
    _t_fail "bootstrap is left non-executable" "still executable"
else
    _t_pass "bootstrap is left non-executable"
fi
assert_file "BOOT_FLAG_SKIP is set" "$MODR/BOOT_FLAG_SKIP"

# --- the bring-up log is what the stick is FOR -----------------------------
assert_contains "the bring-up log is surfaced in the run output" \
    "$OUT" "LAST LINE BEFORE THE HANG"
report=$(find "$DATA" "$ROOT/stick" -name 'restore-*.txt' 2>/dev/null | head -1)
assert_contains "the report carries the full bring-up log" \
    "$(cat "$report" 2>/dev/null)" "LAST LINE BEFORE THE HANG"

finish
