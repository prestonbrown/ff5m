#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
## Pack the AD5X RESCUE stick: AD5X-RESCUE-<stamp>.tgz
##
## A minimal drop-in for the stock updater. It installs NOTHING and never
## reboots: flashforge_restore.sh ships as `flashforge_init.sh`, which is the
## only name stock app_startup.sh will run.
##
## Why this exists separately from build_ad5x_image.sh: a rescue stick must be
## small (it goes on a printer that is already broken, and the stock updater
## copies the whole file to /usr/data first), and it carries no mod payload at
## all - just the restore script, the status frames, and pristine factory copies
## of printer.cfg / printer.base.cfg for the last-resort config restore.
##
## The stock updater extracts with `tar -xvf` into /usr/data/update, so the
## archive must be an UNCOMPRESSED ustar despite the .tgz name.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
AD5X_DIR="$REPO_ROOT/tools/release/ad5x"
: "${OUT_DIR:=$REPO_ROOT/dist}"
# Pristine configs for the last-resort restore. Extracted from the FlashForge
# factory image; empty is allowed (the restore then reports it has no fallback).
: "${FACTORY_PRINTER_CFG:=}"
: "${FACTORY_PRINTER_BASE_CFG:=}"

die() { echo "build_rescue_stick: $*" >&2; exit 1; }

[ -f "$AD5X_DIR/flashforge_restore.sh" ] || die "missing flashforge_restore.sh"
sh -n "$AD5X_DIR/flashforge_restore.sh" || die "flashforge_restore.sh does not parse"

STAMP=$(date +%Y%m%d-%H%M%S)
IMAGE_NAME="AD5X-RESCUE-${STAMP}.tgz"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

install -m 0755 "$AD5X_DIR/flashforge_restore.sh" "$STAGE/flashforge_init.sh"
mkdir -p "$STAGE/img" "$STAGE/cfg"
cp "$AD5X_DIR"/img/*.raw.xz "$STAGE/img/"
[ -n "$FACTORY_PRINTER_CFG" ] && cp "$FACTORY_PRINTER_CFG" "$STAGE/cfg/printer.cfg"
[ -n "$FACTORY_PRINTER_BASE_CFG" ] && cp "$FACTORY_PRINTER_BASE_CFG" "$STAGE/cfg/printer.base.cfg"
printf 'AD5X rescue stick %s\n' "$STAMP" > "$STAGE/version.txt"

mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/$IMAGE_NAME"
tar --format=ustar -cf "$OUT" -C "$STAGE" flashforge_init.sh version.txt img cfg

python3 - "$OUT" <<'PY'
import sys, tarfile
t = tarfile.open(sys.argv[1], mode="r:")   # exactly how the stock updater reads it
names = t.getnames()
assert "flashforge_init.sh" in names, names
print("  verified: uncompressed ustar, %d members" % len(names))
for n in sorted(names):
    m = t.getmember(n)
    if m.isfile():
        print("    %-32s %7d  mode %o" % (n, m.size, m.mode))
PY
echo "build_rescue_stick: wrote $OUT ($(du -h "$OUT" | cut -f1))"
