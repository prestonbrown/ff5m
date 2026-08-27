#!/bin/sh
# init_lib.sh: printer_data provisioning is placed per board.
#
# All off-rig. init_buildroot/fix_config themselves mount and chroot and cannot
# run here, but the two functions carved out for this fix - _init_printer_data
# (the tree) and provision_printer_data (where it goes) - are exercised in
# isolation with a shadowed `uname` (to force PLATFORM) and `ln`/`mount`/`sync`
# faked to record their arguments instead of touching the filesystem. That is
# the same trick ad5x_bootstrap_test uses. Wrapped in `bash -c` because
# init_lib.sh is a bash script.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

INIT_LIB="$REPO_DIR/.shell/init_lib.sh"
assert_file "init_lib.sh exists" "$INIT_LIB"

BASH_BIN=$(command -v bash 2>/dev/null)
if [ -z "$BASH_BIN" ]; then
    echo "ok     bash not installed, skipping init_lib tests"
    finish
fi

# Record every `ln` the tree build issues, for a forced arch and a dest dir.
capture_tree() {
    ARCH="$1" DEST="$2" "$BASH_BIN" -c '
        uname() { echo "$ARCH"; }
        . "$1"
        ln() { echo "ln $*"; }
        mkdir() { :; }
        _init_printer_data "$DEST"
    ' _ "$INIT_LIB" 2>&1
}

# Record provision_printer_data'\''s dispatch (which dest, whether it binds).
capture_provision() {
    ARCH="$1" "$BASH_BIN" -c '
        uname() { echo "$ARCH"; }
        . "$1"
        _init_printer_data() { echo "build $*"; }
        mount() { echo "mount $*"; }
        sync() { :; }
        provision_printer_data
    ' _ "$INIT_LIB" 2>&1
}

# --- tree contents: the logs target is the crux of the fix -------------------
# AD5X $LOG_DIR=/usr/data/logs is invisible in the chroot; it must become
# /data/logs (reachable via the $DATA_MNT bind), NOT the raw host path.
ad5x_tree=$(capture_tree mips /usr/data/.mod/.forge-x/root/printer_data)
assert_contains "ad5x logs -> /data/logs (chroot-reachable, not the host /usr/data/logs)" \
    "$ad5x_tree" "ln -fns /data/logs /usr/data/.mod/.forge-x/root/printer_data/logs"
case "$ad5x_tree" in
    *"/usr/data/logs "*) _t_fail "ad5x logs symlink avoids the raw host path" "raw /usr/data/logs present" ;;
    *)                    _t_pass "ad5x logs symlink avoids the raw host path" ;;
esac
assert_contains "ad5x gcodes -> /data/gcodes (matches klipper virtual_sdcard /usr/data/gcodes)" \
    "$ad5x_tree" "ln -fns /data/gcodes /usr/data/.mod/.forge-x/root/printer_data/gcodes"
assert_contains "ad5x config -> /opt/config/ (symlink, so relative includes resolve)" \
    "$ad5x_tree" "ln -fns /opt/config/ /usr/data/.mod/.forge-x/root/printer_data/config"

# AD5M must stay byte-identical: $LOG_DIR=/data/logFiles already lives under
# /data, so /data/$(basename) is the same string it always had.
ad5m_tree=$(capture_tree armv7l /root/printer_data)
assert_contains "ad5m logs -> /data/logFiles (unchanged)" \
    "$ad5m_tree" "ln -fns /data/logFiles /root/printer_data/logs"
assert_contains "ad5m gcodes -> /data (unchanged)" \
    "$ad5m_tree" "ln -fns /data /root/printer_data/gcodes"

# --- dispatch: location + bind differ per board ------------------------------
ad5x_prov=$(capture_provision mips)
assert_contains "ad5x builds printer_data in the chroot's own /root" \
    "$ad5x_prov" "build /usr/data/.mod/.forge-x/root/printer_data"
case "$ad5x_prov" in
    *mount*) _t_fail "ad5x does not bind-mount printer_data" "a mount was issued" ;;
    *)       _t_pass "ad5x does not bind-mount printer_data (host /root is read-only squashfs)" ;;
esac

ad5m_prov=$(capture_provision armv7l)
assert_contains "ad5m builds printer_data on the host /root" \
    "$ad5m_prov" "build /root/printer_data"
assert_contains "ad5m bind-mounts the host printer_data into the chroot" \
    "$ad5m_prov" "mount --bind /root/printer_data /data/.mod/.forge-x/root/printer_data"

finish
