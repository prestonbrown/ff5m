#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Is this rootfs one of OURS?
#
#   check_rootfs.sh <rootfs.tar.xz>
#
# Exit 0  known-good (ours) or, with ALLOW_UNPINNED_ROOTFS=1, an unknown
#         one that was explicitly allowed through.
# Exit 1  forbidden (tracked foreign blob) or unreadable.
# Exit 2  unknown identity and no override - the default for anything not
#         recorded in rootfs.md5, so a stale or borrowed artifact cannot
#         slip into a release unnoticed.
#
# The identity list lives in rootfs.md5 beside this script; ROOTFS_MD5_FILE
# redirects it for tests.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
MD5_FILE=${ROOTFS_MD5_FILE:-$HERE/rootfs.md5}

rootfs=${1:-}
[ -n "$rootfs" ] || { echo "usage: $0 <rootfs.tar.xz>" >&2; exit 1; }
[ -f "$rootfs" ] || { echo "check_rootfs: no such file: $rootfs" >&2; exit 1; }
[ -f "$MD5_FILE" ] || { echo "check_rootfs: no identity list: $MD5_FILE" >&2; exit 1; }

md5=$(md5sum "$rootfs")
md5=${md5%% *}

entry=$(awk -v key="$md5" \
    '$1 == key && $2 != "" && $1 !~ /^#/ {print $2; exit}' "$MD5_FILE")

case ${entry:-unknown} in
    KNOWN)
        echo "rootfs $md5: known (ours)"
        exit 0
        ;;
    FORBIDDEN)
        note=$(awk -v key="$md5" '$1 == key {print; exit}' "$MD5_FILE")
        echo "check_rootfs: REFUSED - $note" >&2
        echo "This rootfs is tracked as foreign. Build ours instead:" >&2
        echo "  forgex-buildroot (buildroot external tree) -> build.sh ad5x" >&2
        echo "  then append the new md5 to tools/release/rootfs.md5" >&2
        exit 1
        ;;
    *)
        if [ "${ALLOW_UNPINNED_ROOTFS:-0}" = 1 ]; then
            echo "rootfs $md5: UNKNOWN, allowed by override" >&2
            exit 0
        fi
        echo "check_rootfs: unknown rootfs $md5 (not in $MD5_FILE)." >&2
        echo "If this is a fresh forgex-buildroot build, record it there;" >&2
        echo "set ALLOW_UNPINNED_ROOTFS=1 to force this one build." >&2
        exit 2
        ;;
esac
