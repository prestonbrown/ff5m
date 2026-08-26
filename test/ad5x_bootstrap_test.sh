#!/bin/sh
# AD5X boot bootstrap: install/uninstall reversibility and run ordering.
#
# All off-rig. install/uninstall are exercised against a fixture app_startup.sh
# (pointed at with AD5X_APP_STARTUP); the platform guard and the run ordering are
# exercised in-process with a shadowed `uname`, the same trick platform_vars_test
# uses, wrapped in `bash -c` because ad5x_bootstrap.sh is a bash script.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

BOOTSTRAP="$REPO_DIR/.shell/ad5x_bootstrap.sh"
MARKER='# forge-x'

assert_file "bootstrap script exists" "$BOOTSTRAP"

# The script and its tests are bash (BASH_SOURCE, local, arrays). On a box
# without bash there is nothing to exercise, so skip rather than fail - the same
# posture shellcheck_test takes for a missing shellcheck.
BASH_BIN=$(command -v bash 2>/dev/null)
if [ -z "$BASH_BIN" ]; then
    echo "ok     bash not installed, skipping ad5x_bootstrap tests"
    finish
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
FIX="$WORK/app_startup.sh"
PRISTINE="$WORK/pristine"

# A stock-shaped app_startup.sh that launches klippy through klipper/start.sh.
make_fixture() {
    cat > "$FIX" <<'EOF'
#!/bin/sh
echo "stock preamble"
cd /usr/prog
sh klipper/start.sh
echo "stock tail"
EOF
    cp "$FIX" "$PRISTINE"
    rm -f "$FIX.orig"
}

# Invoke a subcommand against the fixture, under bash, quietly.
bs() {
    AD5X_APP_STARTUP="$FIX" "$BASH_BIN" "$BOOTSTRAP" "$@" >/dev/null 2>&1
}

# Call run_bootstrap in-process with a forced `uname -m`. Prints run's combined
# output; returns run's exit status.
run_forced() {
    _arch="$1"; shift
    FORCE_ARCH="$_arch" "$BASH_BIN" -c '
        uname() { echo "$FORCE_ARCH"; }
        . "$1"; shift
        run_bootstrap "$@"
    ' _ "$BOOTSTRAP" "$@" 2>&1
}

# --- install idempotency + placement ----------------------------------------
make_fixture
bs install
bs install
count=$(grep -c -F -- "$MARKER" "$FIX")
assert_eq "install twice injects exactly one hook line" "1" "$count"

placement=$(awk '
    /# forge-x/ { h = NR }
    /klipper\/start\.sh/ && !s { s = NR }
    END { if (h > 0 && s > 0 && h < s) print "before"; else print "after" }
' "$FIX")
assert_eq "hook is placed before stock klipper/start.sh" "before" "$placement"

# --- .orig preservation ------------------------------------------------------
make_fixture
bs install
assert_file "install creates <target>.orig" "$FIX.orig"
if cmp -s "$FIX.orig" "$PRISTINE"; then
    _t_pass ".orig is the pristine original"
else
    _t_fail ".orig is the pristine original" "differs from pre-install file"
fi

# A mutation to .orig must survive a second install (no clobber of the original).
printf 'SENTINEL\n' >> "$FIX.orig"
bs install
if tail -n 1 "$FIX.orig" | grep -Fq SENTINEL; then
    _t_pass "second install does not overwrite .orig"
else
    _t_fail "second install does not overwrite .orig" ".orig was rewritten"
fi

# --- uninstall restores byte-for-byte, and is a no-op otherwise -------------
make_fixture
bs install
bs uninstall
if cmp -s "$FIX" "$PRISTINE"; then
    _t_pass "uninstall restores a byte-identical original"
else
    _t_fail "uninstall restores a byte-identical original" "differs from pristine"
fi

cp "$FIX" "$WORK/before_noop"
bs uninstall
if cmp -s "$FIX" "$WORK/before_noop"; then
    _t_pass "uninstall is a no-op when not installed"
else
    _t_fail "uninstall is a no-op when not installed" "file was modified"
fi

# --- PLATFORM guard ----------------------------------------------------------
if run_forced armv7l --dry-run >/dev/null 2>&1; then
    _t_fail "run refuses when platform is not ad5x" "permitted on ad5m"
else
    _t_pass "run refuses when platform is not ad5x (ad5m)"
fi

if run_forced mips --dry-run >/dev/null 2>&1; then
    _t_pass "run is permitted on ad5x"
else
    _t_fail "run is permitted on ad5x" "refused on ad5x"
fi

# --- dry-run order + no ARM landmines ---------------------------------------
out=$(run_forced mips --dry-run)

steps=$(printf '%s\n' "$out" \
    | sed -n -E 's/^\[dry-run\] ([0-9])\. .*/\1/p' \
    | tr '\n' ' ' | sed 's/ *$//')
assert_eq "dry-run emits steps 1..9 in order" "1 2 3 4 5 6 7 8 9" "$steps"

# ARM-only stages that stay skipped. zstart_klipper is NOT here: stock
# app_startup.sh never launches klippy, so the mod must, and it is a real step.
for bad in S55boot boot_mcu netd tone.py init_swap; do
    case "$out" in
        *"$bad"*) _t_fail "dry-run omits skipped stage: $bad" "present in dry-run output" ;;
        *)        _t_pass "dry-run omits skipped stage: $bad" ;;
    esac
done

# The klippy launch and the stock-UI stop are real steps now.
case "$out" in
    *zstart_klipper*) _t_pass "dry-run launches klipper (zstart_klipper)" ;;
    *)                _t_fail "dry-run launches klipper (zstart_klipper)" "absent" ;;
esac
case "$out" in
    *"stop stock UI"*) _t_pass "dry-run stops the stock UI (firmwareExe)" ;;
    *)                 _t_fail "dry-run stops the stock UI (firmwareExe)" "absent" ;;
esac

# --- failsafe: arm / disarm / one-shot skip gate ----------------------------
# The failsafe functions read the two flag paths that common.sh derives from
# $MOD_ROOT. Off-rig we source the bootstrap and point those globals at fixture
# files, then exercise each function directly.
FS_DIR="$WORK/failsafe"
mkdir -p "$FS_DIR"
FS_FAIL="$FS_DIR/BOOT_FLAG_FAILURE"
FS_SKIP="$FS_DIR/BOOT_FLAG_SKIP"

fs_call() {
    FS_FAIL="$FS_FAIL" FS_SKIP="$FS_SKIP" FN="$1" "$BASH_BIN" -c '
        . "$1"
        BOOT_FAILURE_F="$FS_FAIL"
        BOOT_SKIP_F="$FS_SKIP"
        "$FN"
    ' _ "$BOOTSTRAP" >/dev/null 2>&1
}

# arm sets the failure flag; disarm clears it.
rm -f "$FS_FAIL" "$FS_SKIP"
fs_call failsafe_arm
assert_file "failsafe_arm sets the failure flag" "$FS_FAIL"
fs_call failsafe_disarm
if [ ! -e "$FS_FAIL" ]; then
    _t_pass "failsafe_disarm clears the failure flag"
else
    _t_fail "failsafe_disarm clears the failure flag" "flag still present"
fi

# A prior failure flag makes the gate skip once, and clears the flag.
: > "$FS_FAIL"; rm -f "$FS_SKIP"
if fs_call failsafe_should_skip; then
    if [ ! -e "$FS_FAIL" ]; then
        _t_pass "failsafe skips and clears on a prior failure"
    else
        _t_fail "failsafe skips and clears on a prior failure" "flag not cleared"
    fi
else
    _t_fail "failsafe skips and clears on a prior failure" "gate did not skip"
fi

# An explicit skip flag makes the gate skip once, and clears the flag.
rm -f "$FS_FAIL"; : > "$FS_SKIP"
if fs_call failsafe_should_skip; then
    if [ ! -e "$FS_SKIP" ]; then
        _t_pass "failsafe skips and clears on an explicit skip request"
    else
        _t_fail "failsafe skips and clears on an explicit skip request" "flag not cleared"
    fi
else
    _t_fail "failsafe skips and clears on an explicit skip request" "gate did not skip"
fi

# A clean boot (no flags) does not skip.
rm -f "$FS_FAIL" "$FS_SKIP"
if fs_call failsafe_should_skip; then
    _t_fail "failsafe does not skip a clean boot" "gate skipped with no flag set"
else
    _t_pass "failsafe does not skip a clean boot"
fi

# The dry-run plan documents the failsafe.
case "$out" in
    *failsafe*) _t_pass "dry-run documents the failsafe" ;;
    *)          _t_fail "dry-run documents the failsafe" "no failsafe line in dry-run output" ;;
esac

finish
