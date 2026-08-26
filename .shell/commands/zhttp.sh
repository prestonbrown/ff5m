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

## Auxiliary script for web interface changing
##
## Copyright (C) 2025, Alexander K <https://github.com/drA1ex>
## Copyright (C) 2025, Sergei Rozhkov <https://github.com/ghzserg>
##
## This file may be distributed under the terms of the GNU GPLv3 license

# Board-specific values. platform.sh rather than common.sh: this file needs
# only the descriptor, not common.sh's bash helpers.
# shellcheck disable=SC1091
. /opt/config/mod/.shell/platform.sh

CFG_SCRIPT="$MOD_ROOT/.shell/commands/zconf.sh"
CFG_PATH="/opt/config/mod_data/web.conf"

DEFAULT_WEB="fluidd"


load() {
    # Create default configuration if needed
    if [ ! -f "$CFG_PATH" ]; then
        cp "$MOD_ROOT/.cfg/default/web.conf" "$CFG_PATH"
    fi
    
    WEB=$($CFG_SCRIPT $CFG_PATH --get "CLIENT" "$DEFAULT_WEB")
}

switch() {
    if [ "$WEB" = "$DEFAULT_WEB" ]; then
        WEB="mainsail"
    else
        WEB="$DEFAULT_WEB"
    fi
    
    $CFG_SCRIPT $CFG_PATH --set CLIENT="$WEB"
    
    sync
}

apply() {
    cat > "$MOD"/root/www/index.html <<EOF
<html>
<body>
    <script>window.location.href = './$WEB';</script>
    <p>If you are not redirected automatically, follow this <a href="./$WEB">link</a>.</p>
</body>
</html>
EOF
    
    sync
}

restart() {
    unset LD_PRELOAD
    chroot "$MOD" "$MOD_ROOT/.root/S70httpd" restart
}


case "$1" in
    switch)
        load
        switch
        apply
        
        restart
    ;;
    apply)
        load
        apply
    ;;
    restart)
        restart
    ;;
    status)
        load
        echo "Current WebUI selected: $WEB"
    ;;
    *)
        echo "Usage: $0 (apply|switch|restart|status)"
        exit 1
    ;;
esac

exit $?