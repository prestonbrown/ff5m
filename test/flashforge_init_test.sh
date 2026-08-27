#!/bin/sh
# AD5X Stage-B installer (tools/release/ad5x/flashforge_init.sh) guard + unpack tests.
#
# All off-rig. The installer paints /dev/fb0, reads df/mount, unpacks into
# /usr/data, and sysrq-reboots - none of which a test box can do. It exposes a
# handful of env seams that default to the real device values and are only
# redirected here (FF_FB, FF_DATA_MNT, FF_UNAME, FF_FREE_KB, FF_MOUNTS,
# FF_NO_REBOOT, FF_SKIP_INSTALL), so the guards and the unpack can be exercised
# against a fake root with a fake mount table and a forced arch/free-space.
#
# The guard cases run the FULL path (FF_SKIP_INSTALL unset): a correct build
# exits at the guard before any unpack, so removing a guard makes the installer
# reach the unpack and create a target dir - which the "unpacks nothing"
# assertions then catch. The unpack + md5 cases need xz/tar/md5sum to build a
# real staged payload and are skipped when those are absent, the same posture
# the static-analysis and bootstrap suites take for a missing tool.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

INSTALLER="$REPO_DIR/tools/release/ad5x/flashforge_init.sh"
assert_file "installer exists" "$INSTALLER"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# --- fixed seam inputs shared by every case ---------------------------------
FB="$WORK/fb.out"

# Fake mount tables for the already-running guard.
MOUNTS_CLEAN="$WORK/mounts.clean"
MOUNTS_FORGEX="$WORK/mounts.forgex"
MOUNTS_ZMOD="$WORK/mounts.zmod"
printf '/dev/mmcblk0p7 /usr/data ext4 rw 0 0\n' > "$MOUNTS_CLEAN"
printf '/dev/mmcblk0p7 /usr/data ext4 rw 0 0\nproc /usr/data/.mod/.forge-x/proc proc rw 0 0\n' > "$MOUNTS_FORGEX"
printf '/dev/mmcblk0p7 /usr/data ext4 rw 0 0\nproc /usr/data/.mod/.zmod/proc proc rw 0 0\n' > "$MOUNTS_ZMOD"

# --- optional real staged payload (needs the archive tools) -----------------
HAVE_TOOLS=0
STAGE="$WORK/stage"
if command -v xz >/dev/null 2>&1 && command -v tar >/dev/null 2>&1 && command -v md5sum >/dev/null 2>&1; then
    HAVE_TOOLS=1
    mkdir -p "$STAGE/xz"

    # buildroot rootfs: one marker file so we can prove it landed in $MOD.
    _b="$WORK/br"; mkdir -p "$_b"; echo rootfs > "$_b/ROOTFS_MARKER"
    ( cd "$_b" && tar -cf - . ) | xz -c > "$STAGE/xz/buildroot.tar.xz"

    # mod tree: one marker file. Deliberately NO .shell/ad5x_bootstrap.sh, so
    # the installer's `[ -x "$BOOTSTRAP" ]` is false and the hook step no-ops
    # instead of touching a real stock file.
    _m="$WORK/mod"; mkdir -p "$_m"; echo modtree > "$_m/MOD_MARKER"
    ( cd "$_m" && tar -cf - . ) | xz -c > "$STAGE/xz/data.tar.xz"

    # entware: a 0-byte stub, to exercise the `[ -s ]` skip branch.
    : > "$STAGE/xz/entware.tar.xz"

    echo "forgex-test-1.0.0" > "$STAGE/version.txt"
    echo "# stub common.sh"  > "$STAGE/common.sh"
    cp "$INSTALLER" "$STAGE/flashforge_init.sh"

    # md5.list must cover every member md5sum -c will see, with paths relative
    # to $WORK_DIR. Generated from the staged files so it always matches.
    ( cd "$STAGE" && md5sum flashforge_init.sh common.sh version.txt \
        xz/buildroot.tar.xz xz/data.tar.xz xz/entware.tar.xz > md5.list )
fi

# The installer derives $WORK_DIR from its own path, so the md5/unpack path must
# run the STAGED copy (whose dir holds md5.list + xz/). Guard-only failures
# never read $WORK_DIR, so they run fine from either location.
if [ "$HAVE_TOOLS" -eq 1 ]; then
    RUN_SCRIPT="$STAGE/flashforge_init.sh"
else
    RUN_SCRIPT="$INSTALLER"
fi

# --- harness ----------------------------------------------------------------

# Fresh fake data root per case so an "unpacked nothing" assertion is meaningful.
new_data() {
    DATA="$WORK/data.$1"
    rm -rf "$DATA"
    mkdir -p "$DATA"
    MOD_DIR="$DATA/.mod/.forge-x"
    MOD_ROOT_DIR="$DATA/config/mod"
}

# Run the installer as the stock updater does (MACHINE PID as $1 $2), under the
# redirected fake root + seams. Sets OUT (combined output) and RC (exit status).
# Reads the case vars: ARCH, FREE_KB, MOUNTS, SKIP, and RUN_SCRIPT.
run_installer() {
    _m="$1"; _p="$2"
    OUT=$(env \
        FF_FB="$FB" \
        FF_DATA_MNT="$DATA" \
        FF_UNAME="$ARCH" \
        FF_FREE_KB="$FREE_KB" \
        FF_MOUNTS="$MOUNTS" \
        FF_SKIP_INSTALL="$SKIP" \
        FF_NO_REBOOT=1 \
        sh "$RUN_SCRIPT" "$_m" "$_p" 2>&1) && RC=0 || RC=$?
}

assert_no_unpack() {
    if [ -d "$MOD_DIR" ] || [ -d "$MOD_ROOT_DIR" ]; then
        _t_fail "$1" "an unpack target dir was created"
    else
        _t_pass "$1"
    fi
}

assert_not_contains() {
    case "$2" in
        *"$3"*) _t_fail "$1" "output unexpectedly contains '$3'" ;;
        *)      _t_pass "$1" ;;
    esac
}

# ---------------------------------------------------------------------------
# Guard 1 - machine / PID / arch. Full path enabled (SKIP empty).
# ---------------------------------------------------------------------------
new_data mismatch
ARCH=mips; FREE_KB=2000000; MOUNTS="$MOUNTS_CLEAN"; SKIP=
run_installer WRONGMACH 0026
assert_eq       "wrong MACHINE exits 0 (lets stock boot)" "0" "$RC"
assert_contains "wrong MACHINE is reported"               "$OUT" "does not match"
assert_not_contains "wrong MACHINE never reaches guards-passed" "$OUT" "guards passed"
assert_no_unpack "wrong MACHINE unpacks nothing"

new_data badpid
ARCH=mips; FREE_KB=2000000; MOUNTS="$MOUNTS_CLEAN"; SKIP=
run_installer AD5X 9999
assert_eq       "wrong PID exits 0"        "0" "$RC"
assert_contains "wrong PID is reported"    "$OUT" "does not match"
assert_no_unpack "wrong PID unpacks nothing"

new_data badarch
ARCH=armv7l; FREE_KB=2000000; MOUNTS="$MOUNTS_CLEAN"; SKIP=
run_installer AD5X 0026
assert_eq       "non-mips arch exits 0"     "0" "$RC"
assert_contains "non-mips arch is reported" "$OUT" "does not match"
assert_no_unpack "non-mips arch unpacks nothing"

# ---------------------------------------------------------------------------
# Guard 2 - a mod already mounted/running. Must refuse hard (exit 1).
# ---------------------------------------------------------------------------
new_data forgexmount
ARCH=mips; FREE_KB=2000000; MOUNTS="$MOUNTS_FORGEX"; SKIP=
run_installer AD5X 0026
assert_eq       "running Forge-X refuses with exit 1" "1" "$RC"
assert_contains "running mod is reported"             "$OUT" "already running"
assert_no_unpack "running Forge-X unpacks nothing"

new_data zmodmount
ARCH=mips; FREE_KB=2000000; MOUNTS="$MOUNTS_ZMOD"; SKIP=
run_installer AD5X 0026
assert_eq       "running ZMOD also refuses with exit 1" "1" "$RC"
assert_no_unpack "running ZMOD unpacks nothing"

# ---------------------------------------------------------------------------
# Guard 3 - free space. 512 MB == 524228 KiB is the exact threshold.
# ---------------------------------------------------------------------------
new_data nospace
ARCH=mips; FREE_KB=1000; MOUNTS="$MOUNTS_CLEAN"; SKIP=
run_installer AD5X 0026
assert_eq       "low free space exits 0"     "0" "$RC"
assert_contains "low free space is reported" "$OUT" "need"
assert_not_contains "low free space never reaches guards-passed" "$OUT" "guards passed"
assert_no_unpack "low free space unpacks nothing"

# One KiB under the threshold must still be refused (pins the -lt comparison).
new_data underthreshold
ARCH=mips; FREE_KB=524227; MOUNTS="$MOUNTS_CLEAN"; SKIP=
run_installer AD5X 0026
assert_eq       "one KiB under threshold is refused (exit 0)" "0" "$RC"
assert_not_contains "under threshold never reaches guards-passed" "$OUT" "guards passed"

# A non-numeric df reading is coerced to 0 and must fail the space guard.
new_data junkfree
ARCH=mips; FREE_KB=notanumber; MOUNTS="$MOUNTS_CLEAN"; SKIP=1
run_installer AD5X 0026
assert_eq       "unreadable free space fails safe (exit 0)" "0" "$RC"
assert_not_contains "unreadable free space never reaches guards-passed" "$OUT" "guards passed"

# ---------------------------------------------------------------------------
# Happy path - all guards pass. SKIP=1 stops right after the guards, proving
# a valid environment is NOT wrongly blocked (and exercising the skip seam).
# ---------------------------------------------------------------------------
new_data happy
ARCH=mips; FREE_KB=2000000; MOUNTS="$MOUNTS_CLEAN"; SKIP=1
run_installer AD5X 0026
assert_eq       "valid environment passes the guards (exit 0)" "0" "$RC"
assert_contains "valid environment reaches guards-passed"      "$OUT" "guards passed"

# Exactly the threshold (524228 KiB) must PASS the space guard.
new_data atthreshold
ARCH=mips; FREE_KB=524228; MOUNTS="$MOUNTS_CLEAN"; SKIP=1
run_installer AD5X 0026
assert_contains "exactly the 512 MB threshold passes the guard" "$OUT" "guards passed"

# ---------------------------------------------------------------------------
# Full install + md5 - need the archive tools; skip cleanly without them.
# ---------------------------------------------------------------------------
if [ "$HAVE_TOOLS" -eq 1 ]; then
    # A verified payload unpacks both trees, skips the empty entware stub, and
    # reaches the (suppressed) reboot.
    new_data install
    ARCH=mips; FREE_KB=2000000; MOUNTS="$MOUNTS_CLEAN"; SKIP=
    run_installer AD5X 0026
    assert_eq   "verified payload installs and exits 0"   "0" "$RC"
    assert_file "buildroot rootfs unpacked into \$MOD"    "$MOD_DIR/ROOTFS_MARKER"
    assert_file "mod tree unpacked into \$MOD_ROOT"       "$MOD_ROOT_DIR/MOD_MARKER"
    assert_contains "version is announced"                "$OUT" "forgex-test-1.0.0"
    assert_contains "empty entware stub is skipped"       "$OUT" "Entware stub is empty"
    assert_contains "install reaches the (suppressed) reboot" "$OUT" "zreboot suppressed"

    # A corrupted member must fail md5 and abort BEFORE any unpack.
    STAGE_BAD="$WORK/stage_bad"
    cp -r "$STAGE" "$STAGE_BAD"
    printf 'corrupt\n' >> "$STAGE_BAD/xz/buildroot.tar.xz"
    new_data md5fail
    ARCH=mips; FREE_KB=2000000; MOUNTS="$MOUNTS_CLEAN"; SKIP=
    RUN_SCRIPT="$STAGE_BAD/flashforge_init.sh"
    run_installer AD5X 0026
    RUN_SCRIPT="$STAGE/flashforge_init.sh"
    assert_eq       "md5 mismatch exits 0"      "0" "$RC"
    assert_contains "md5 mismatch is reported"  "$OUT" "md5 verification failed"
    assert_no_unpack "md5 mismatch unpacks nothing"
else
    _t_pass "xz/tar/md5sum absent, skipping full-install + md5 cases"
fi

finish
