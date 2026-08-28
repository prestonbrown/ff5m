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

# --- placement on the REAL stock AD5X app_startup.sh shape ------------------
#
# The fixture above carries a klipper/start.sh line, but the shipped AD5X file
# does not: on this board the Qt UI launches klippy ("/usr/prog/klipper/start.sh
# &" is a literal inside firmwareExe), so app_startup.sh names it nowhere. The
# insert-before branch therefore never fires on a real printer and the hook is
# appended - which is also where ZMOD appends its own prepare.sh. Pin that, so
# the append fallback is covered rather than assumed.
cat > "$FIX" <<'EOF'
#!/bin/sh
echo "stock preamble"
/usr/prog/PROGRAM/software/firmwareExe -1 -D -qws &
sleep 10
count=`ps |grep firmwareExe |grep -v "grep" |wc -l`
if [ 0 == $count ];then
	/usr/prog/PROGRAM/software/firmwareExe -1 -D -qws &
fi
EOF
cp "$FIX" "$WORK/stock_shape"
rm -f "$FIX.orig"
bs install
assert_eq "stock-shaped file: exactly one hook line" "1" "$(grep -c -F -- "$MARKER" "$FIX")"
if tail -n 1 "$FIX" | grep -Fq -- "$MARKER"; then
    _t_pass "stock-shaped file: hook is appended as the last line"
else
    _t_fail "stock-shaped file: hook is appended as the last line" \
            "last line is: $(tail -n 1 "$FIX")"
fi
bs uninstall
if cmp -s "$FIX" "$WORK/stock_shape"; then
    _t_pass "stock-shaped file: uninstall restores it byte-identically"
else
    _t_fail "stock-shaped file: uninstall restores it byte-identically" "differs"
fi

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

# The host has no /bin/bash; the plan must document providing it (Step 2).
case "$out" in
    *"provide /bin/bash"*) _t_pass "dry-run documents providing /bin/bash" ;;
    *)                     _t_fail "dry-run documents providing /bin/bash" "absent" ;;
esac

# The AD5X has no working Feather UI; the plan must document defaulting the
# "Try Feather" web-UI promo off (Step 2 variables.cfg seeding).
case "$out" in
    *show_feather_promo*) _t_pass "dry-run documents disabling the Feather promo" ;;
    *)                    _t_fail "dry-run documents disabling the Feather promo" "absent" ;;
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

# --- _ensure_var: mod_params-safe value quoting -----------------------------
# variables.cfg is read by Klipper's [mod_params] via ast.literal_eval, so a
# non-numeric seed value must be a quoted string literal or klippy halts at
# config load. Exercised through the real zconf.sh against a temp variables.cfg.
EV=$(mktemp -d)
printf '[Variables]\n' > "$EV/vars.cfg"
ev_seed() {
    KEY="$1" VAL="$2" VP="$EV/vars.cfg" RD="$REPO_DIR" "$BASH_BIN" -c '
        . "$1"
        CMDS="$RD/.shell/commands"
        VAR_PATH="$VP"
        _ensure_var "$KEY" "$VAL"
    ' _ "$BOOTSTRAP" >/dev/null 2>&1
}
ev_seed display HEADLESS
ev_seed use_swap OFF
ev_seed filament_switch_sensor 0
evc=$(cat "$EV/vars.cfg")
assert_contains "enum value seeded as a quoted literal (mod_params-safe)" "$evc" "display='HEADLESS'"
assert_contains "second enum value quoted too" "$evc" "use_swap='OFF'"
assert_contains "numeric value seeded bare (already a valid literal)" "$evc" "filament_switch_sensor=0"
case "$evc" in
    *"filament_switch_sensor='0'"*) _t_fail "numeric value is not over-quoted" "0 was wrapped in quotes" ;;
    *)                              _t_pass "numeric value is not over-quoted" ;;
esac
rm -rf "$EV"

# --- bring-up watchdog ------------------------------------------------------
#
# The bring-up runs synchronously inside stock app_startup.sh, so an unbounded
# hang takes the whole printer down. The watchdog must actually fire, and must
# actually stand down when the run completes - a watchdog that never fires and
# one that always fires are both silent failures.

# Arm a 2s watchdog, then hang for 30s. Should die in ~2-4s, not 30.
_wd_start=$(date +%s)
AD5X_BRINGUP_LIMIT=2 "$BASH_BIN" -c '
    . "$1"
    watchdog_arm
    sleep 30
' _ "$BOOTSTRAP" >/dev/null 2>&1
_wd_elapsed=$(( $(date +%s) - _wd_start ))
if [ "$_wd_elapsed" -lt 10 ]; then
    _t_pass "watchdog kills a bring-up that overruns its limit (${_wd_elapsed}s)"
else
    _t_fail "watchdog kills a bring-up that overruns its limit" \
            "still alive after ${_wd_elapsed}s"
fi

# Disarmed watchdog must NOT kill a run that finished inside the limit.
_wd_out=$(AD5X_BRINGUP_LIMIT=2 "$BASH_BIN" -c '
    . "$1"
    watchdog_arm
    watchdog_disarm
    sleep 4
    echo SURVIVED
' _ "$BOOTSTRAP" 2>/dev/null)
assert_contains "disarmed watchdog does not kill a completed bring-up" \
    "$_wd_out" "SURVIVED"

# A limit of 0 disables the watchdog entirely (escape hatch).
_wd_out=$(AD5X_BRINGUP_LIMIT=0 "$BASH_BIN" -c '
    . "$1"
    watchdog_arm
    sleep 3
    echo SURVIVED
' _ "$BOOTSTRAP" 2>/dev/null)
assert_contains "AD5X_BRINGUP_LIMIT=0 disables the watchdog" "$_wd_out" "SURVIVED"


# --- klippy launch: detached, with a bounded socket wait --------------------
#
# zstart_klipper.sh execs the STOCK /usr/prog/klipper/start.sh on this board,
# which ends in `klipperDaemon start`. Both other consumers detach it (stock
# firmwareExe: "start.sh &"; ZMOD: start-stop-daemon -S -b), so a foreground
# call here can hold the bring-up forever and Step 8 never runs. Point CMDS at a
# launcher that never returns and require launch_klipper to come back anyway.
LK=$(mktemp -d)
mkdir -p "$LK/commands"
cat > "$LK/commands/zstart_klipper.sh" <<'EOF'
#!/bin/sh
sleep 30
EOF
chmod +x "$LK/commands/zstart_klipper.sh"

_lk_start=$(date +%s)
_lk_out=$(CMDS="$LK/commands" "$BASH_BIN" -c '
    . "$1"
    CMDS="$2"
    launch_klipper
    echo "RETURNED pid=${KLIPPER_LAUNCH_PID:-none}"
' _ "$BOOTSTRAP" "$LK/commands" 2>/dev/null)
_lk_elapsed=$(( $(date +%s) - _lk_start ))
if [ "$_lk_elapsed" -lt 5 ]; then
    _t_pass "launch_klipper detaches a launcher that never returns (${_lk_elapsed}s)"
else
    _t_fail "launch_klipper detaches a launcher that never returns" \
            "blocked for ${_lk_elapsed}s"
fi
assert_contains "launch_klipper records the launched pid" "$_lk_out" "RETURNED pid="
case "$_lk_out" in
    *"pid=none"*) _t_fail "launch_klipper records the launched pid" "pid was empty" ;;
    *)            _t_pass "launch_klipper records a non-empty pid" ;;
esac

# An already-present socket must be seen at once, not slept through.
: > "$LK/uds"
_ws_start=$(date +%s)
_ws_out=$(AD5X_KLIPPY_UDS="$LK/uds" AD5X_KLIPPY_WAIT=20 "$BASH_BIN" -c '
    . "$1"
    wait_for_klippy_socket
' _ "$BOOTSTRAP" 2>/dev/null)
_ws_elapsed=$(( $(date +%s) - _ws_start ))
if [ "$_ws_elapsed" -lt 3 ]; then
    _t_pass "wait_for_klippy_socket returns at once when the socket exists (${_ws_elapsed}s)"
else
    _t_fail "wait_for_klippy_socket returns at once when the socket exists" \
            "took ${_ws_elapsed}s"
fi
assert_contains "socket-up path is logged" "$_ws_out" "Klipper socket up"

# A missing socket must time out and still succeed: Moonraker retries, so this
# is a diagnostic, not a boot failure.
_ws_start=$(date +%s)
AD5X_KLIPPY_UDS="$LK/absent" AD5X_KLIPPY_WAIT=2 "$BASH_BIN" -c '
    . "$1"
    wait_for_klippy_socket
' _ "$BOOTSTRAP" >/dev/null 2>&1
_ws_rc=$?
_ws_elapsed=$(( $(date +%s) - _ws_start ))
assert_eq "wait_for_klippy_socket is non-fatal on timeout" "0" "$_ws_rc"
if [ "$_ws_elapsed" -ge 2 ] && [ "$_ws_elapsed" -lt 8 ]; then
    _t_pass "wait_for_klippy_socket honours its limit (${_ws_elapsed}s for a 2s limit)"
else
    _t_fail "wait_for_klippy_socket honours its limit" \
            "waited ${_ws_elapsed}s for a 2s limit"
fi

# The timeout message must discriminate the two failures, because they need
# different next steps: a launcher still running with no socket is klipperDaemon
# not returning; a launcher already gone means klippy started and died.
_ws_err=$(CMDS="$LK/commands" AD5X_KLIPPY_UDS="$LK/absent" AD5X_KLIPPY_WAIT=2 \
    "$BASH_BIN" -c '
    . "$1"
    CMDS="$2"
    launch_klipper
    wait_for_klippy_socket
' _ "$BOOTSTRAP" "$LK/commands" 2>&1 >/dev/null)
assert_contains "timeout names a launcher that is still running" \
    "$_ws_err" "still running"

_ws_err=$(AD5X_KLIPPY_UDS="$LK/absent" AD5X_KLIPPY_WAIT=2 "$BASH_BIN" -c '
    . "$1"
    KLIPPER_LAUNCH_PID=""
    wait_for_klippy_socket
' _ "$BOOTSTRAP" 2>&1 >/dev/null)
assert_contains "timeout with no live launcher points at the klippy log" \
    "$_ws_err" "already exited"

# Limit 0 disables the wait entirely (escape hatch, same shape as the watchdog).
_ws_start=$(date +%s)
AD5X_KLIPPY_UDS="$LK/absent" AD5X_KLIPPY_WAIT=0 "$BASH_BIN" -c '
    . "$1"
    wait_for_klippy_socket
' _ "$BOOTSTRAP" >/dev/null 2>&1
_ws_elapsed=$(( $(date +%s) - _ws_start ))
if [ "$_ws_elapsed" -lt 2 ]; then
    _t_pass "AD5X_KLIPPY_WAIT=0 disables the socket wait"
else
    _t_fail "AD5X_KLIPPY_WAIT=0 disables the socket wait" "waited ${_ws_elapsed}s"
fi
rm -rf "$LK"


# --- bring-up log -----------------------------------------------------------
#
# The hook runs inside stock app_startup.sh, whose stdout goes nowhere, and
# every service that could carry a log starts in Step 8. Without this file a
# failed boot leaves no evidence at all - which is why the first hang needed a
# bespoke USB stick to diagnose.
BL=$(mktemp -d)

_bl_run() {
    AD5X_BRINGUP_LOG_DIR="$1" AD5X_BRINGUP_LOG_KEEP="${2:-3}" "$BASH_BIN" -c '
        . "$1"
        bringup_log_start
        echo "BODY-$2"
    ' _ "$BOOTSTRAP" "${3:-x}" 2>&1
}

_bl_stdout=$(_bl_run "$BL/log" 3 one)
assert_file "bringup_log_start creates the log" "$BL/log/ad5x_bootstrap.log"
assert_contains "bring-up output goes to the log" \
    "$(cat "$BL/log/ad5x_bootstrap.log")" "BODY-one"
assert_contains "the log is stamped with a start header" \
    "$(cat "$BL/log/ad5x_bootstrap.log")" "bring-up start"
case "$_bl_stdout" in
    *BODY-one*) _t_fail "bring-up output is redirected, not echoed" "BODY-one reached stdout" ;;
    *)          _t_pass "bring-up output is redirected, not echoed" ;;
esac

# A second run must rotate, not append: each boot gets its own record.
_bl_run "$BL/log" 3 two >/dev/null
assert_contains "previous run rotates to .1" \
    "$(cat "$BL/log/ad5x_bootstrap.log.1")" "BODY-one"
assert_contains "current run is the live log" \
    "$(cat "$BL/log/ad5x_bootstrap.log")" "BODY-two"

# Rotation is bounded, so the log dir cannot grow without limit on a device
# that boot-loops.
_bl_run "$BL/log" 3 three >/dev/null
_bl_run "$BL/log" 3 four >/dev/null
_bl_count=$(find "$BL/log" -name 'ad5x_bootstrap.log*' | wc -l)
assert_eq "rotation keeps at most KEEP files" "3" "$_bl_count"
if [ -e "$BL/log/ad5x_bootstrap.log.3" ]; then
    _t_fail "rotation does not exceed KEEP" ".3 exists with KEEP=3"
else
    _t_pass "rotation does not exceed KEEP"
fi

# KEEP=1 means no rotation at all, so the log must be truncated each boot
# rather than appended to - otherwise it grows without bound on a device that
# reboots often.
_bl_run "$BL/one" 1 alpha >/dev/null
_bl_run "$BL/one" 1 beta  >/dev/null
_bl_one=$(cat "$BL/one/ad5x_bootstrap.log")
assert_contains "KEEP=1 keeps the latest run" "$_bl_one" "BODY-beta"
case "$_bl_one" in
    *BODY-alpha*) _t_fail "KEEP=1 truncates instead of appending" "the previous run is still in the log" ;;
    *)            _t_pass "KEEP=1 truncates instead of appending" ;;
esac
assert_eq "KEEP=1 keeps exactly one file" "1" \
    "$(find "$BL/one" -name 'ad5x_bootstrap.log*' | wc -l)"

# An unwritable log dir must not take the boot down with it. `exec >>` on a file
# that cannot be opened is fatal to a non-interactive shell, so the guard has to
# come first.
if [ "$(id -u)" = "0" ]; then
    echo "ok     running as root, skipping the unwritable-log-dir case"
else
    mkdir -p "$BL/ro"
    chmod 555 "$BL/ro"
    _bl_stdout=$(_bl_run "$BL/ro/nested" 3 five)
    _bl_rc=$?
    assert_eq "unwritable log dir is not fatal" "0" "$_bl_rc"
    assert_contains "bring-up keeps running with no log" "$_bl_stdout" "BODY-five"
    chmod 755 "$BL/ro"
fi
rm -rf "$BL"

finish
