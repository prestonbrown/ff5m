#!/bin/sh
# check_rootfs.sh (tools/release) identity gate tests.
#
# The gate exists so a release build cannot embed a foreign rootfs silently:
# the early AD5X bring-up borrowed ZMOD's release rootfs, and a rebuild done
# from habit rather than intent would still be shipping it. Each case builds
# its own tiny fixture rootfs and records that fixture's REAL md5 into a
# fixture identity list, so the assertions exercise the md5 matching itself
# rather than trusting a hand-copied digest.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

CHECK="$REPO_DIR/tools/release/check_rootfs.sh"
assert_file "check script exists" "$CHECK"

command -v md5sum >/dev/null 2>&1 || {
    echo "skip: md5sum absent"; finish; exit 0; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Three fixtures: one we record KNOWN, one we record FORBIDDEN, one we never
# record. Distinct contents, so the md5s cannot collide.
printf 'ours\n'   > "$WORK/ours.tar.xz"
printf 'theirs\n' > "$WORK/theirs.tar.xz"
printf 'mystery\n' > "$WORK/mystery.tar.xz"

ours_md5=$(md5sum "$WORK/ours.tar.xz"); ours_md5=${ours_md5%% *}
theirs_md5=$(md5sum "$WORK/theirs.tar.xz"); theirs_md5=${theirs_md5%% *}

LIST="$WORK/rootfs.md5"
{
    echo "# fixture identity list"
    echo "$ours_md5  KNOWN      fixture build"
    echo "$theirs_md5  FORBIDDEN  fixture foreign blob"
} > "$LIST"

run_check() {
    # Diagnostics (refusal reasons) go to stderr; merge so assertions see them.
    ROOTFS_MD5_FILE="$LIST" "$CHECK" "$1" 2>&1
}

# --- ours passes and says so ------------------------------------------------
out=$(ROOTFS_MD5_FILE="$LIST" "$CHECK" "$WORK/ours.tar.xz" 2>/dev/null)
assert_eq "known rootfs exits 0" "$?" 0
assert_contains "known rootfs names its identity" "$out" "$ours_md5"

# --- the foreign blob is refused, loudly ------------------------------------
out=$(run_check "$WORK/theirs.tar.xz"); rc=$?
assert_ne "forbidden rootfs does not pass" "$rc" 0
assert_contains "refusal points at forgex-br" "$out" "forgex-br"
assert_contains "refusal names the tracked entry" "$out" "FORBIDDEN"

# --- unknown needs the override ---------------------------------------------
out=$(run_check "$WORK/mystery.tar.xz"); rc=$?
assert_eq "unknown rootfs exits 2 without override" "$rc" 2
assert_contains "unknown says how to proceed" "$out" "ALLOW_UNPINNED_ROOTFS"

out=$(ALLOW_UNPINNED_ROOTFS=1 ROOTFS_MD5_FILE="$LIST" \
      "$CHECK" "$WORK/mystery.tar.xz" 2>/dev/null)
assert_eq "override lets an unknown rootfs through" "$?" 0

# --- the real list: our hardware-validated rootfs is KNOWN, the borrow FORBIDDEN
REAL="$REPO_DIR/tools/release/rootfs.md5"
out=$(ROOTFS_MD5_FILE="$REAL" "$CHECK" "$WORK/ours.tar.xz" 2>/dev/null) \
    && assert_ne "fixture is not in the real list" "$?" 0 \
    || assert_eq "fixture refused by the real list too" "$?" 2

finish
