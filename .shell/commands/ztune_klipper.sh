#!/bin/sh
# shellcheck shell=bash
# forge-x bash trampoline. This is a bash script, but the AD5X stock host ships
# no /bin/bash (read-only squashfs, BusyBox), so a #!/bin/bash shebang cannot
# start it. Run under #!/bin/sh and, when bash is absent, re-exec under the
# mod's own bash via the rootfs loader (FORGEX_BASH_* from platform.sh). A no-op
# indirection where /bin/bash exists (AD5M: one extra exec), and skipped when
# the file is sourced (the test harness sources it).
if [ "${_FORGEX_BASHED:-}" != "$0" ] && { [ -z "${BASH_SOURCE:-}" ] || [ "${BASH_SOURCE:-}" = "$0" ]; }; then
    _FORGEX_BASHED="$0"; export _FORGEX_BASHED
    if command -v bash >/dev/null 2>&1; then exec bash "$0" "$@"; fi
    _fx_dir=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
    if [ -f "$_fx_dir/platform.sh" ]; then _fx_p="$_fx_dir/platform.sh"; else _fx_p="$_fx_dir/../platform.sh"; fi
    # shellcheck source=/dev/null
    . "$_fx_p"
    exec "$FORGEX_BASH_LD" --library-path "$FORGEX_BASH_LIBPATH" "$FORGEX_BASH_BIN" "$0" "$@"
fi

source /opt/config/mod/.shell/common.sh

TOOLHEAD_F="$KLIPPER_DIR/klippy/toolhead.py"

already_done_e17() {
    echo "Move Queue Overflow (E0017) already in sync!"
}

fix_disable_e17() {
    echo "Reverting LOOKAHEAD_FLUSH_TIME"

    grep -qe "^LOOKAHEAD_FLUSH_TIME = 0.5" "$TOOLHEAD_F" \
        && already_done_e17 && return
    
    sed -i 's|^LOOKAHEAD_FLUSH_TIME = .*|LOOKAHEAD_FLUSH_TIME = 0.5|' "$TOOLHEAD_F"

    sync
    echo "Done"
}

fix_enable_e17() {
    echo "Patching LOOKAHEAD_FLUSH_TIME"

    grep -qe "^LOOKAHEAD_FLUSH_TIME = 0.150" "$TOOLHEAD_F" \
        && already_done_e17 && return
    
    sed -i 's|^LOOKAHEAD_FLUSH_TIME = .*|LOOKAHEAD_FLUSH_TIME = 0.150|' "$TOOLHEAD_F"

    sync
    echo "Done"
}

fix_disable_all() {
    fix_disable_e17
}

fix_enable_all() {
    fix_enable_e17
}

fix_apply() {
    local enabled="$($CFG_SCRIPT "$VAR_PATH" --get "fix_e0017" "MISSING")"
    if [ "$enabled" = 'MISSING' ]; then
        enabled="$($CFG_SCRIPT "$VAR_PATH" --get "tune_klipper" "0")"
    fi

    if [ "$enabled" == "0" ]; then
        fix_disable_all
    else
        fix_enable_all
    fi
}

case "$1" in
    0)
        fix_disable_all
    ;;
    1)
        fix_enable_all
    ;;
    apply)
        fix_apply
    ;;
    *)
        echo "Command not supported"
        exit 1
esac
