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

## Update configuration file
##
## Copyright (C) 2025, Alexander K <https://github.com/drA1ex>erg>
##
## This file may be distributed under the terms of the GNU GPLv3 license


read_param() {
    local key="$1"
    local default="$2"
    
    if [ -z "$key" ]; then
        echo "Error: Empty key in --get mode"
        exit 1
    fi
    
    if grep -qE "^${key}\s*=\s*" "$CONFIG_FILE"; then
        value=$(sed -n -E "s/^$key\s*=\s*(.*)/\1/p" "$CONFIG_FILE")
        
        # Check if the value starts and ends with a single quote
        if [[ "$value" =~ ^\'.*\'$ ]]; then
            value="${value:1:-1}"
        fi

        echo "$value"
    else
        echo "$default"
    fi
}

update_param() {
    local key=$1
    local value=$2

    local existing=$(read_param "$key" "__NOT_EXISTS")

    if [ "$existing" == "__NOT_EXISTS" ]; then
        echo "Adding \"$key\" = \"$value\""
        echo "$key=$value" >> "$CONFIG_FILE"

    elif [ "$value" != "$existing" ]; then
        echo "Setting \"$key\" = \"$value\""
        sed -i -E "s|^($key\s*=\s*).*|\1$value|" "$CONFIG_FILE"
    fi
}

update_config() {
    cp -f "$CONFIG_FILE" "$CONFIG_FILE.bak"
    
    for arg in "$@"; do
        # Check if argument matches 'KEY=VALUE' pattern using grep
        if ! echo "$arg" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*='; then
            echo "Warning: Invalid parameter assignment \"$arg\""
            continue
        fi

        key="${arg%%=*}"
        value="${arg#*=}"
        
        update_param "$key" "$value"
    done
    
    sync
}

usage() {
    echo "Usage: $0 (--get <key> [default] | --set <key=value ...>)"
}

CONFIG_FILE="$1"
shift

if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: File \"$CONFIG_FILE\" doesn't exists"
    usage
    exit 1
fi

case "$1" in
    --get)
        read_param "$2" "$3"
    ;;
    --set)
        shift
        update_config "$@"
    ;;
    *)
        usage
        exit 1
    ;;
esac

exit $?
