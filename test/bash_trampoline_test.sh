#!/bin/sh
# Bash-trampoline behavior.
#
# The trampoline prelude lets a #!/bin/bash mod script run on a host that has no
# /bin/bash (AD5X stock: read-only squashfs, BusyBox). It is #!/bin/sh, and when
# bash is absent re-execs the script under the mod's own bash via the rootfs
# loader. This suite exercises all branches off-rig with fixtures, so the prelude
# never has to be trusted on faith on the printer.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

# The prelude re-execs under bash; without a bash to re-exec to there is nothing
# to test. Same skip posture the other suites take for a missing tool.
BASH_BIN=$(command -v bash 2>/dev/null)
if [ -z "$BASH_BIN" ]; then
    echo "ok     bash not installed, skipping bash_trampoline tests"
    finish
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Single source of truth: lift the actual prelude (lines 1..first `fi`) from a
# shipped trampolined script, so a drift in the real prelude drifts the fixture.
PRELUDE=$(sed -n '1,/^fi$/p' "$REPO_DIR/.shell/commands/zconf.sh")
case "$PRELUDE" in
    *_FORGEX_BASHED*) : ;;
    *) _t_fail "prelude lifted from zconf.sh" "no trampoline found in zconf.sh"; finish ;;
esac

# A fixture = the real prelude + a body that reports which interpreter ran it.
make_fixture() {
    _dir="$1"
    mkdir -p "$_dir"
    {
        printf '%s\n' "$PRELUDE"
        echo 'echo "BODY_RAN under=${BASH_VERSION:+bash}${BASH_VERSION:-sh} arg=$1"'
    } > "$_dir/fix.sh"
    chmod +x "$_dir/fix.sh"
}

# --- 1. executed, bash present -> body runs under bash -----------------------
make_fixture "$WORK/a"
out=$("$WORK/a/fix.sh" hello 2>/dev/null)
case "$out" in
    *"under=bash"*"arg=hello"*) _t_pass "executed with bash: body runs under bash" ;;
    *) _t_fail "executed with bash: body runs under bash" "got: $out" ;;
esac

# --- 2. sourced -> trampoline skipped, no re-exec, caller survives -----------
# A re-exec would replace the sourcing shell; CALLER_ALIVE proves it did not.
make_fixture "$WORK/b"
out=$(bash -c '. "$1"/b/fix.sh sourced_arg; echo CALLER_ALIVE' _ "$WORK" 2>/dev/null)
case "$out" in
    *BODY_RAN*CALLER_ALIVE*) _t_pass "sourced: trampoline skipped, caller survives" ;;
    *) _t_fail "sourced: trampoline skipped, caller survives" "got: $out" ;;
esac

# --- 3. executed, NO /bin/bash -> re-exec via $MOD/$ROOTFS_LD ----------------
# Fake a bash-less host: a PATH without bash, a platform.sh beside the fixture
# giving MOD + ROOTFS_LD, and a fake loader that drops `--library-path <p>` and
# execs the real bash it was handed - exactly the shape the prelude invokes.
D="$WORK/c"
make_fixture "$D"
mkdir -p "$D/root/lib" "$D/root/bin"
cat > "$D/platform.sh" <<EOF
FORGEX_BASH_LD=$D/root/lib/fake-ld.so
FORGEX_BASH_LIBPATH=$D/root/lib
FORGEX_BASH_BIN=$D/root/bin/bash
EOF
cat > "$D/root/lib/fake-ld.so" <<'EOF'
#!/bin/sh
# emulate: LD --library-path <path> <bash> <script> <args...>
shift 2
exec "$@"
EOF
chmod +x "$D/root/lib/fake-ld.so"
ln -s "$BASH_BIN" "$D/root/bin/bash"

# A PATH holding only the externals the prelude needs (dirname), never bash.
FAKEBIN="$WORK/nobash-bin"
mkdir -p "$FAKEBIN"
ln -s "$(command -v dirname)" "$FAKEBIN/dirname"
SH_BIN=$(command -v sh)
out=$(PATH="$FAKEBIN" "$SH_BIN" "$D/fix.sh" viald 2>/dev/null)
case "$out" in
    *"under=bash"*"arg=viald"*) _t_pass "executed without /bin/bash: re-execs rootfs bash via loader" ;;
    *) _t_fail "executed without /bin/bash: re-execs rootfs bash via loader" "got: $out" ;;
esac

# --- 4. cross-call guard: an inherited flag from a DIFFERENT script must not
#        suppress trampolining (else a bash helper called by another bash script
#        would run its bash body under sh). ------------------------------------
make_fixture "$WORK/d"
out=$(_FORGEX_BASHED="/some/other/script" "$WORK/d/fix.sh" x 2>/dev/null)
case "$out" in
    *"under=bash"*) _t_pass "cross-call: inherited flag for another script still re-execs" ;;
    *) _t_fail "cross-call: inherited flag for another script still re-execs" "got: $out" ;;
esac

finish
