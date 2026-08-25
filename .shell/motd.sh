#!/bin/bash

## motd generator
##
## Copyright (C) 2025, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

# S00init runs this as a child process, so the descriptor is not inherited:
# common.sh exports nothing. Without this, $PLATFORM_NAME expands to empty.
# shellcheck disable=SC1090,SC1091
. /opt/config/mod/.shell/common.sh

MOD_VERSION=${1-$(cat /opt/config/mod/version.txt)}
FIRMWARE_VERSION=${2-$(cat /root/version)}
VERSION_PATCH=${3-$(cat /tmp/version_patch 2> /dev/null || echo "")}

centered() {
    local text="$1"; local width=$2; local offset=${3:-0}
    
    local p_text=$(echo -ne "$text" | sed $'s/\x1b\[[0-9;]*m//g')
    local length=${#p_text}
    
    local left_padding=$(( ($width - ${length}) / 2 ))
    local right_padding=$(( $offset + $width - ${length} - $left_padding ))
    
    printf "%*s%s%*s" $left_padding "" "$text" $right_padding ""
}

# Calculate special offset since old bash versions can't correctly calculate emoji as 1 length
_S="🔥"
_OFFSET=$(( ${#_S} - 1 ))

MOD_TEXT=$(centered "🔥 \033[1;36mFORGE-X v${MOD_VERSION}" 35 $_OFFSET)
FF_TEXT=$(centered "\033[1;33m⚡ \033[36m${PLATFORM_NAME} v${FIRMWARE_VERSION}" 35 $_OFFSET)
PATCH_TEXT=$(centered "\033[1;36m${VERSION_PATCH}" 35 0)

echo -e "\033[35m


 ███████╗ ██╗ █████╗ ███████╗██╗  ██╗███████╗ ██████╗ ██████╗  ██████╗ ███████╗
 ██╔════╝███║██╔══██╗██╔════╝██║  ██║██╔════╝██╔═████╗██╔══██╗██╔════╝ ██╔════╝
 █████╗  ╚██║███████║███████╗███████║█████╗  ██║██╔██║██████╔╝██║  ███╗█████╗
 ██╔══╝   ██║██╔══██║╚════██║██╔══██║██╔══╝  ████╔╝██║██╔══██╗██║   ██║██╔══╝
 ██║      ██║██║  ██║███████║██║  ██║██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗
 ╚═╝      ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝██║      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝
                                     ██║
 ${MOD_TEXT}\033[0;35m██║  ${FF_TEXT}\033[0;35m
 ${PATCH_TEXT} \033[0;35m╚═╝

\033[0m"