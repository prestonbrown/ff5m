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
# klippy runs on the host; bind the chroot /root (which holds the tree just
# built) over the host's empty read-only /root so host /root/printer_data
# resolves for klippy's gcode_shell_command paths, sharing one tree with the
# chroot's Moonraker.
assert_contains "ad5x binds the chroot /root over the host /root" \
    "$ad5x_prov" "mount --bind /usr/data/.mod/.forge-x/root /root"

ad5m_prov=$(capture_provision armv7l)
assert_contains "ad5m builds printer_data on the host /root" \
    "$ad5m_prov" "build /root/printer_data"
assert_contains "ad5m bind-mounts the host printer_data into the chroot" \
    "$ad5m_prov" "mount --bind /root/printer_data /data/.mod/.forge-x/root/printer_data"

# --- apply_platform_macros: board-specific macro selection ------------------
# Overrides ship as macros/<name>.$PLATFORM.cfg and are copied over the default.
# AD5X selects its .ad5x variants; AD5M has none, so its defaults stay in force.
run_platform_macros() {
    ARCH="$1" WORK="$2" "$BASH_BIN" -c '
        uname() { echo "$ARCH"; }
        . "$1"
        MOD_ROOT="$WORK"
        apply_platform_macros
    ' _ "$INIT_LIB" 2>&1
}

FXA=$(mktemp -d); mkdir -p "$FXA/macros"
printf "AD5M-DEFAULT\n" > "$FXA/macros/hw_base.cfg"
printf "AD5X-OVERRIDE\n" > "$FXA/macros/hw_base.ad5x.cfg"
run_platform_macros mips "$FXA" >/dev/null
assert_eq "ad5x selects hw_base.ad5x.cfg over hw_base.cfg" \
    "AD5X-OVERRIDE" "$(cat "$FXA/macros/hw_base.cfg")"
assert_eq "ad5x leaves the .ad5x source untouched" \
    "AD5X-OVERRIDE" "$(cat "$FXA/macros/hw_base.ad5x.cfg")"
rm -rf "$FXA"

FXM=$(mktemp -d); mkdir -p "$FXM/macros"
printf "AD5M-DEFAULT\n" > "$FXM/macros/hw_base.cfg"
printf "AD5X-OVERRIDE\n" > "$FXM/macros/hw_base.ad5x.cfg"
run_platform_macros armv7l "$FXM" >/dev/null
assert_eq "ad5m leaves hw_base.cfg default (no .ad5m override to select)" \
    "AD5M-DEFAULT" "$(cat "$FXM/macros/hw_base.cfg")"
rm -rf "$FXM"

# --- provide_host_bash: AD5X /bin/bash provider ------------------------------
# The AD5X host has no /bin/bash; provide_host_bash builds a faithful /bin
# superset (a copy of /bin plus the host-bin/bash trampoline) and binds it over
# /bin. Exercised with a shadowed uname and HOST_BASH_PROBE forced at a missing
# path, so the guard does not short-circuit on the dev host's own /bin/bash;
# cp/mount/mkdir/rm/chmod are faked to record their arguments.
run_provide_bash() {
    ARCH="$1" PROBE="$2" "$BASH_BIN" -c '
        uname() { echo "$ARCH"; }
        . "$1"
        HOST_BASH_PROBE="$PROBE"
        rm() { echo "rm $*"; }
        mkdir() { echo "mkdir $*"; }
        cp() { echo "cp $*"; }
        chmod() { echo "chmod $*"; }
        mount() { echo "mount $*"; }
        provide_host_bash
    ' _ "$INIT_LIB" 2>&1
}

# ad5x, no host bash: copy the real /bin, install the trampoline, bind over /bin.
ad5x_bash=$(run_provide_bash mips /does/not/exist)
assert_contains "ad5x copies the real /bin into the superset" \
    "$ad5x_bash" "cp -a /bin/. /usr/data/.mod/host-bin/"
assert_contains "ad5x installs the bash trampoline into the superset" \
    "$ad5x_bash" "cp /usr/data/config/mod/.shell/host-bin/bash /usr/data/.mod/host-bin/bash"
assert_contains "ad5x binds the superset over /bin" \
    "$ad5x_bash" "mount --bind /usr/data/.mod/host-bin /bin"

# ad5x, host already provides bash: the guard short-circuits, nothing is built.
ad5x_have=$(run_provide_bash mips /bin/sh)   # /bin/sh exists on the dev host
case "$ad5x_have" in
    *mount*|*"cp -a"*) _t_fail "ad5x no-ops when /bin/bash already exists" "built/bound anyway: $ad5x_have" ;;
    *)                 _t_pass "ad5x no-ops when /bin/bash already exists" ;;
esac

# ad5m: never provides bash (host has it); returns before any work.
ad5m_bash=$(run_provide_bash armv7l /does/not/exist)
case "$ad5m_bash" in
    *mount*|*"cp -a"*) _t_fail "ad5m no-ops (host already has /bin/bash)" "did work: $ad5m_bash" ;;
    *)                 _t_pass "ad5m no-ops (host already has /bin/bash)" ;;
esac

# --- host-bin/bash trampoline: forwards through the rootfs loader ------------
SHIM="$REPO_DIR/.shell/host-bin/bash"
assert_file "host-bin/bash trampoline exists" "$SHIM"

# Behavioral: copy the shim, repoint its hardcoded descriptor path at a fixture
# platform.sh, and run it through a fake loader that drops `--library-path <p>`
# and execs the bash it was handed - exactly the shape the shim invokes. Proves
# the shim runs the rootfs bash with the script's arguments intact. Skipped
# when there is no bash to re-exec to (BASH_BIN empty -> handled by the skip at
# the top of this file).
SB=$(mktemp -d)
mkdir -p "$SB/root/lib" "$SB/root/bin"
cat > "$SB/platform.sh" <<EOF
FORGEX_BASH_LD=$SB/root/lib/fake-ld.so
FORGEX_BASH_LIBPATH=$SB/root/lib
FORGEX_BASH_BIN=$SB/root/bin/bash
EOF
cat > "$SB/root/lib/fake-ld.so" <<'EOF'
#!/bin/sh
# emulate: LD --library-path <path> <bash> <script> <args...>
shift 2
exec "$@"
EOF
chmod +x "$SB/root/lib/fake-ld.so"
ln -s "$BASH_BIN" "$SB/root/bin/bash"
sed "s#/usr/data/config/mod/.shell/platform.sh#$SB/platform.sh#" "$SHIM" > "$SB/bash"
chmod +x "$SB/bash"
printf '#!/bin/bash\necho SHIM_RAN under=${BASH_VERSION:+bash} arg=$1\n' > "$SB/script.sh"
chmod +x "$SB/script.sh"
shimout=$("$SB/bash" "$SB/script.sh" hello 2>/dev/null)
case "$shimout" in
    *"under=bash"*"arg=hello"*) _t_pass "shim runs the rootfs bash with args intact" ;;
    *)                          _t_fail "shim runs the rootfs bash with args intact" "got: $shimout" ;;
esac
rm -rf "$SB"

finish
