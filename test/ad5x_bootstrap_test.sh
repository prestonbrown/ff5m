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
assert_eq "dry-run emits steps 1..8 in order" "1 2 3 4 5 6 7 8" "$steps"

for bad in S55boot boot_mcu zstart_klipper netd tone.py init_swap; do
    case "$out" in
        *"$bad"*) _t_fail "dry-run omits skipped stage: $bad" "present in dry-run output" ;;
        *)        _t_pass "dry-run omits skipped stage: $bad" ;;
    esac
done

finish
