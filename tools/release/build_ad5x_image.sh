#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
## Build the AD5X Stage-B flashable OTA image: AD5X-ForgeX-<version>.tgz
##
## The output is an UNCOMPRESSED POSIX (ustar) tar despite the .tgz name - that
## is exactly what .py/zupdate.py's verify_posix_tar() opens (mode="r:") and
## what the stock FlashForge updater extracts. Its members:
##
##   flashforge_init.sh   the installer (tools/release/ad5x/flashforge_init.sh)
##   common.sh            copy of .shell/common.sh (contract member)
##   version.txt          the Forge-X version string
##   md5.list             md5sums (paths relative to the tar root) the installer
##                        checks with `md5sum -c` before it unpacks anything
##   xz/data.tar.xz       the mod payload (this repo's runtime tree)  -> $MOD_ROOT
##   xz/buildroot.tar.xz  the MIPS chroot rootfs                      -> $MOD
##   xz/entware.tar.xz    /opt (a 0-byte stub for the first proof)    -> /opt
##   img/forgex-*.raw.xz  flash-time status frames (extra members; painted by
##                        the installer - tolerated by the superset contract)
##
## The rootfs is not built here. Point BUILDROOT_TAR at one produced by
## forgex-br (our buildroot external tree; see ROOTFS.md), or give none and
## fetch_rootfs.sh acquires the one rootfs.pin names - downloaded, or built
## from source with BUILD_ROOTFS=1. NO_AUTO_ROOTFS=1 forbids that and demands
## BUILDROOT_TAR. However it arrives, its md5 is verified against rootfs.md5:
## our builds are listed KNOWN, the early bring-up borrow is listed FORBIDDEN,
## and anything unknown needs ALLOW_UNPINNED_ROOTFS=1 - so a release cannot
## silently embed a foreign or stale rootfs.
set -euo pipefail
exec 3>&1 1>&2   # progress/logs -> stderr; final artifact path -> stdout (fd3)

# tools/release/ -> repo root
REPO_ROOT=$(cd "$(dirname "$0")/.." && cd .. && pwd)
AD5X_DIR="$REPO_ROOT/tools/release/ad5x"

# ---- inputs (all overridable via env) -------------------------------------
# BUILDROOT_TAR: the xz-compressed MIPS chroot rootfs tarball that becomes
# xz/buildroot.tar.xz. Empty means "go and get the pinned one" (below); there
# is no path default, because a committed script must not embed a path into
# someone's extracted third-party release.
: "${BUILDROOT_TAR:=}"
# ENTWARE_TAR optional: a real mipsel Entware tarball. Empty => 0-byte stub, so
# the installer's `[ -s ]` guard skips it cleanly.
: "${ENTWARE_TAR:=}"
: "${OUT_DIR:=$REPO_ROOT/dist}"

# The mod payload: the repo's runtime tree that installs to $MOD_ROOT. Matches
# the M0/M1 bring-up payload plus .cfg.ad5x (the AD5X config seam). Dev-only
# top-level entries (.github docs test tests tools sync*.sh README ...) are
# deliberately excluded. Built from the WORKING TREE so an uncommitted change
# under test is captured; a release build simply has everything committed.
PAYLOAD_ITEMS=".bin .cfg .cfg.ad5x .py .root .shell .zsh KAMP config macros \
mod_params.json moonraker.conf sql telegram tuning.cfg version.txt"

die() { echo "build_ad5x_image: $*" >&2; exit 1; }

# ---- the rootfs: supplied, or acquired --------------------------------------
# fetch_rootfs.sh writes the path it acquired to stdout and everything else to
# stderr, which is left to flow through: when acquisition fails, its message -
# which pin it read, and how to proceed by hand - is the one worth reading.
rootfs_from_source=0
if [ -z "$BUILDROOT_TAR" ]; then
    if [ "${NO_AUTO_ROOTFS:-0}" = 1 ]; then
        die "BUILDROOT_TAR is required (path to the MIPS rootfs.tar.xz): \
NO_AUTO_ROOTFS=1 forbids acquiring one. Build it with forgex-br: see tools/release/ROOTFS.md."
    fi
    echo "build_ad5x_image: no BUILDROOT_TAR given; acquiring the pinned rootfs."
    echo "  This needs network access. BUILD_ROOTFS=1 builds it from source"
    echo "  instead, whose first run compiles a cross toolchain and is slow."
    echo "  NO_AUTO_ROOTFS=1 turns this off and requires BUILDROOT_TAR instead."
    if ! BUILDROOT_TAR=$("$REPO_ROOT/tools/release/fetch_rootfs.sh"); then
        die "could not acquire a rootfs."
    fi
    if [ "${BUILD_ROOTFS:-0}" = 1 ]; then rootfs_from_source=1; fi
fi
[ -f "$BUILDROOT_TAR" ] || die "BUILDROOT_TAR not found: $BUILDROOT_TAR"

# Whose rootfs is this? check_rootfs.sh owns the answer (exit 1 foreign,
# 2 unpinned without override, 0 known/allowed); its output carries the why.
# A rootfs built from source moments ago cannot be recorded in rootfs.md5 yet,
# so it - and only it - is waved through: a download has to match the pin, and
# a tarball the caller handed us answers to the gate unassisted.
if [ "$rootfs_from_source" = 1 ]; then
    export ALLOW_UNPINNED_ROOTFS=1
fi
if ! check_out=$("$REPO_ROOT/tools/release/check_rootfs.sh" "$BUILDROOT_TAR" 2>&1); then
    die "rootfs check refused BUILDROOT_TAR:
$check_out"
fi
echo "  $check_out"
[ -f "$AD5X_DIR/flashforge_init.sh" ] || die "installer missing: $AD5X_DIR/flashforge_init.sh"
[ -f "$REPO_ROOT/.shell/common.sh" ] || die "common.sh missing"
[ -f "$REPO_ROOT/version.txt" ] || die "version.txt missing"

VERSION=$(tr -d ' \t\r\n' < "$REPO_ROOT/version.txt")
[ -n "$VERSION" ] || die "version.txt is empty"
IMAGE_NAME="AD5X-ForgeX-${VERSION}.tgz"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/xz" "$STAGE/img"

echo "build_ad5x_image: version=$VERSION -> $IMAGE_NAME"

# ---- xz/data.tar.xz : the mod payload from the working tree ----------------
echo "  data.tar.xz     <- mod payload"
_items=""
for it in $PAYLOAD_ITEMS; do
    if [ -e "$REPO_ROOT/$it" ]; then _items="$_items $it"; else
        echo "    (skip absent payload item: $it)" >&2
    fi
done
[ -n "$_items" ] || die "no payload items found under $REPO_ROOT"
# Exclude Python bytecode caches: they are gitignored working-tree cruft (from
# running the test suite / klippy locally), x86 .pyc that has no business in a
# MIPS firmware image. The device regenerates bytecode from source on first run.
# shellcheck disable=SC2086
tar -C "$REPO_ROOT" --format=ustar \
    --exclude='__pycache__' --exclude='*.pyc' \
    -cf - $_items | xz -T0 -c > "$STAGE/xz/data.tar.xz"

# ---- xz/buildroot.tar.xz : the borrowed/clean MIPS rootfs ------------------
echo "  buildroot.tar.xz<- $BUILDROOT_TAR"
cp "$BUILDROOT_TAR" "$STAGE/xz/buildroot.tar.xz"

# ---- xz/entware.tar.xz : real tarball or 0-byte stub -----------------------
if [ -n "$ENTWARE_TAR" ]; then
    [ -f "$ENTWARE_TAR" ] || die "ENTWARE_TAR not found: $ENTWARE_TAR"
    echo "  entware.tar.xz  <- $ENTWARE_TAR"
    cp "$ENTWARE_TAR" "$STAGE/xz/entware.tar.xz"
else
    echo "  entware.tar.xz  <- 0-byte stub (installer -s guard skips it)"
    : > "$STAGE/xz/entware.tar.xz"
fi

# ---- top-level contract members -------------------------------------------
cp "$AD5X_DIR/flashforge_init.sh" "$STAGE/flashforge_init.sh"
cp "$REPO_ROOT/.shell/common.sh"  "$STAGE/common.sh"
printf '%s\n' "$VERSION"          > "$STAGE/version.txt"

# ---- status frames (extra members) ----------------------------------------
if ls "$AD5X_DIR"/img/forgex-*.raw.xz >/dev/null 2>&1; then
    cp "$AD5X_DIR"/img/forgex-*.raw.xz "$STAGE/img/"
else
    echo "    (warning: no img/forgex-*.raw.xz status frames found)" >&2
fi

# ---- md5.list : covers the payload members the installer verifies ----------
# Paths are relative to the tar root because the installer runs `md5sum -c`
# from WORK_DIR. Cover the three xz members + version.txt + the frames.
( cd "$STAGE" && md5sum version.txt xz/*.tar.xz img/*.raw.xz 2>/dev/null > md5.list )

# ---- assemble the outer UNCOMPRESSED ustar tar -----------------------------
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/$IMAGE_NAME"
tar -C "$STAGE" --format=ustar -cf "$OUT" \
    flashforge_init.sh common.sh version.txt md5.list xz img

echo "build_ad5x_image: wrote $OUT ($(du -h "$OUT" | cut -f1))"
echo "$OUT" >&3
