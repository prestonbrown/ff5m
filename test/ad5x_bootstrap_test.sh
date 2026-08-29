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
HOOK_LATE_ONLY='[ -x /usr/data/config/mod/.shell/ad5x_bootstrap.sh ] && /usr/data/config/mod/.shell/ad5x_bootstrap.sh   # forge-x'

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
mount /usr/prog/etc /etc
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
# Count the LATE hook specifically: "# forge-x" is a prefix of "# forge-x-early",
# so a bare -F count now sees both.
assert_eq "stock-shaped file: exactly one late hook line" "1" "$(grep -c '# forge-x$' "$FIX")"
assert_eq "stock-shaped file: exactly one early hook line" "1" "$(grep -c 'forge-x-early' "$FIX")"
if tail -n 1 "$FIX" | grep -Fq -- "$MARKER"; then
    _t_pass "stock-shaped file: hook is appended as the last line"
else
    _t_fail "stock-shaped file: hook is appended as the last line" \
            "last line is: $(tail -n 1 "$FIX")"
fi

# --- the EARLY hook, and why it exists --------------------------------------
#
# A stood-down mod still has to undo its klipper overlay, and the late hook is a
# boot too late: stock starts firmwareExe ~100 lines earlier, the UI launches
# klipper against the overlaid tree, klippy dies, and the panel sits on
# "Initializing..." while our revert runs seconds afterwards and helps only the
# NEXT boot. Observed on the rig. So the failsafe gets its own hook near the top.
early_line=$(grep -n 'forge-x-early' "$FIX" | cut -d: -f1)
anchor_line=$(grep -n 'mount /usr/prog/etc /etc' "$FIX" | head -1 | cut -d: -f1)
ui_line=$(grep -n 'firmwareExe' "$FIX" | head -1 | cut -d: -f1)
late_line=$(grep -n '# forge-x$' "$FIX" | tail -1 | cut -d: -f1)

if [ -n "$early_line" ]; then
    _t_pass "an early hook is injected"
else
    _t_fail "an early hook is injected" "no forge-x-early line in the file"
fi
if [ -n "$early_line" ] && [ -n "$ui_line" ] && [ "$early_line" -lt "$ui_line" ]; then
    _t_pass "the early hook runs BEFORE the stock UI starts"
else
    _t_fail "the early hook runs BEFORE the stock UI starts" \
            "early=$early_line ui=$ui_line"
fi
if [ -n "$early_line" ] && [ -n "$anchor_line" ] && [ "$early_line" -gt "$anchor_line" ]; then
    _t_pass "the early hook sits just after its anchor"
else
    _t_fail "the early hook sits just after its anchor" "early=$early_line anchor=$anchor_line"
fi
if [ -n "$late_line" ] && [ -n "$early_line" ] && [ "$late_line" -gt "$early_line" ]; then
    _t_pass "the late hook still comes after the early one"
else
    _t_fail "the late hook still comes after the early one" "late=$late_line early=$early_line"
fi
assert_eq "installing twice injects exactly one early hook" "1" \
    "$(grep -c 'forge-x-early' "$FIX")"

# UPGRADE PATH: a printer already carrying the late hook must still get the early
# one. A single "any forge-x marker present -> return" check silently skips it,
# which is precisely the shape a live printer takes on the next install.
cp "$WORK/stock_shape" "$FIX"
rm -f "$FIX.orig"
printf '%s\n' "$HOOK_LATE_ONLY" >> "$FIX"
assert_eq "precondition: late hook only" "0" "$(grep -c 'forge-x-early' "$FIX")"
bs install
assert_eq "upgrade injects the missing early hook" "1" "$(grep -c 'forge-x-early' "$FIX")"
assert_eq "upgrade does not duplicate the late hook" "1" "$(grep -c '# forge-x$' "$FIX")"

# ...and the mirror case: early present, late missing.
cp "$WORK/stock_shape" "$FIX"
rm -f "$FIX.orig"
bs install
grep -v '# forge-x$' "$FIX" > "$FIX.tmp" && mv "$FIX.tmp" "$FIX"
assert_eq "precondition: early hook only" "0" "$(grep -c '# forge-x$' "$FIX")"
bs install
assert_eq "upgrade injects the missing late hook" "1" "$(grep -c '# forge-x$' "$FIX")"
assert_eq "upgrade does not duplicate the early hook" "1" "$(grep -c 'forge-x-early' "$FIX")"

# Both present: a true no-op.
cp "$FIX" "$WORK/both_before"
bs install
if cmp -s "$FIX" "$WORK/both_before"; then
    _t_pass "install is a no-op when both hooks are present"
else
    _t_fail "install is a no-op when both hooks are present" "file changed"
fi

bs uninstall
if cmp -s "$FIX" "$WORK/stock_shape"; then
    _t_pass "stock-shaped file: uninstall restores it byte-identically"
else
    _t_fail "stock-shaped file: uninstall restores it byte-identically" "differs"
fi
case "$(cat "$FIX")" in
    *forge-x*) _t_fail "uninstall removes BOTH hooks" "a forge-x line survived" ;;
    *)         _t_pass "uninstall removes BOTH hooks" ;;
esac

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

# failsafe_peek must NOT consume the flag. The early hook peeks; if it cleared
# the flag, the late hook on the SAME boot would see a clean slate and run the
# bring-up we had just decided to stand down.
: > "$FS_FAIL"
if fs_call failsafe_peek; then
    _t_pass "failsafe_peek sees a stand-down request"
else
    _t_fail "failsafe_peek sees a stand-down request" "peek returned false"
fi
assert_file "failsafe_peek does NOT consume the flag" "$FS_FAIL"
if fs_call failsafe_should_skip; then
    _t_pass "the late hook still consumes it afterwards"
else
    _t_fail "the late hook still consumes it afterwards" "should_skip returned false"
fi
if [ -e "$FS_FAIL" ]; then
    _t_fail "failsafe_should_skip consumes the flag" "flag survived"
else
    _t_pass "failsafe_should_skip consumes the flag"
fi
rm -f "$FS_FAIL" "$FS_SKIP"
if fs_call failsafe_peek; then
    _t_fail "failsafe_peek is false on a clean boot" "peek returned true"
else
    _t_pass "failsafe_peek is false on a clean boot"
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


# --- network takeover -------------------------------------------------------
#
# Step 2 kills the stock UI, and on this board that UI is the network manager:
# `ifconfig eth0 up` and `udhcpc -i eth0 -p /var/run/udhcpc.pid` are literals
# inside firmwareExe, while stock app_startup.sh only ever takes eth0 DOWN. A
# bring-up that does not take the role over leaves a fully working mod that
# nothing can reach - which is exactly how a completed bring-up got mistaken for
# a two-day boot hang. These pin the behaviour that stops that recurring.
#
# ifconfig/udhcpc/ip are shadowed as functions so this runs off-rig with no
# interface touched. NET_IFACES points at a name under /sys/class/net that
# really exists here (lo), so the "present" path is exercised for real.
NET_TRACE=$(mktemp)
net_call() {
    : > "$NET_TRACE"
    # The real call sends udhcpc to /dev/null, so the shadow records itself to a
    # file instead - a redirect inside the function beats the caller's.
    AD5X_NET_IFACES="$1" AD5X_NET_WAIT="$2" NET_FAKE_IP="$3" NET_TRACE="$NET_TRACE" \
    "$BASH_BIN" -c '
        . "$1"
        ifconfig() {
            case "$2" in up) echo "IFCONFIG_UP $1"; return 0 ;; esac
            [ -n "$NET_FAKE_IP" ] && echo "          inet addr:10.0.0.5  Bcast:..."
            return 0
        }
        ip() { return 1; }
        udhcpc() { echo "UDHCPC $*" >> "$NET_TRACE"; }
        sleep() { :; }
        provide_network
    ' _ "$BOOTSTRAP" 2>&1
}

out=$(net_call lo 3 "")
assert_contains "brings a present, unaddressed interface up" "$out" "IFCONFIG_UP lo"
assert_contains "starts a DHCP client on it" "$(cat "$NET_TRACE")" "UDHCPC -i lo"
assert_contains "uses a per-interface pid file" "$(cat "$NET_TRACE")" "/var/run/udhcpc.lo.pid"

# An interface that already has an address must be left completely alone: the
# stock UI may still be managing it, and re-running DHCP would fight it.
out=$(net_call lo 3 yes)
assert_contains "an already-addressed interface is left alone" "$out" "already addressed"
case "$(cat "$NET_TRACE")" in
    *UDHCPC*) _t_fail "no DHCP client on an addressed interface" "udhcpc was started anyway" ;;
    *)        _t_pass "no DHCP client on an addressed interface" ;;
esac

# A name that is not an interface must be skipped, not have ifconfig run at it.
out=$(net_call definitely_not_an_iface 3 "")
assert_contains "an absent interface is skipped" "$out" "not present, skipping"
case "$out" in
    *IFCONFIG_UP*) _t_fail "no ifconfig on an absent interface" "it was brought up anyway" ;;
    *)             _t_pass "no ifconfig on an absent interface" ;;
esac

# Never fatal, and it must say so loudly - everything downstream that looks
# broken will actually be this.
out=$(net_call lo 2 "")
_net_rc=$?
assert_eq "provide_network is non-fatal when no address arrives" "0" "$_net_rc"
assert_contains "a network failure is reported loudly" "$out" "will be unreachable"

# The wait is skippable, same escape-hatch shape as the watchdog and the socket wait.
out=$(net_call lo 0 "")
case "$out" in
    *"will be unreachable"*) _t_fail "AD5X_NET_WAIT=0 skips the wait" "it still waited and complained" ;;
    *)                       _t_pass "AD5X_NET_WAIT=0 skips the wait" ;;
esac

# --- opt-in Wi-Fi -----------------------------------------------------------
# Wi-Fi belongs to HelixScreen's WiFiManager; a boot script that always claimed
# wlan0 would fight it. So the ONLY thing that turns this on is an operator
# leaving credentials at WIFI_CONF. These pin both halves: that it stays off
# with no file, and that it actually associates and gets a lease with one.
WIFI_TRACE=$(mktemp)
WIFI_SYS="$WORK/sysfs"
mkdir -p "$WIFI_SYS/lo" "$WIFI_SYS/wlan0"

# $1 = WIFI_CONF path (may not exist), $2 = wpa_cli state to report
wifi_call() {
    : > "$NET_TRACE"; : > "$WIFI_TRACE"
    AD5X_NET_IFACES=lo AD5X_NET_WAIT=0 AD5X_WIFI_CONF="$1" WPA_STATE="$2" \
    AD5X_NET_SYSFS="$WIFI_SYS" NET_TRACE="$NET_TRACE" WIFI_TRACE="$WIFI_TRACE" \
    "$BASH_BIN" -c '
        . "$1"
        ifconfig() {
            case "$2" in up) echo "IFCONFIG_UP $1"; return 0 ;; esac
            return 0
        }
        ip() { return 1; }
        udhcpc() { echo "UDHCPC $*" >> "$NET_TRACE"; }
        wpa_cli() { [ -n "$WPA_STATE" ] && echo "wpa_state=$WPA_STATE"; return 0; }
        wpa_supplicant() { echo "WPA_SUPPLICANT $*" >> "$WIFI_TRACE"; return 0; }
        sleep() { :; }
        provide_network
    ' _ "$BOOTSTRAP" 2>&1
}

# No credentials: the radio must be untouched, and nothing said about it.
out=$(wifi_call "$WORK/absent.conf" "")
assert_empty "no credentials means no supplicant" "$(cat "$WIFI_TRACE")"
case "$(cat "$NET_TRACE")" in
    *wlan0*) _t_fail "no credentials means no DHCP on wlan0" "udhcpc ran on wlan0" ;;
    *)       _t_pass "no credentials means no DHCP on wlan0" ;;
esac
case "$out" in
    *"Wi-Fi credentials"*) _t_fail "silent when Wi-Fi is not opted in" "it announced Wi-Fi anyway" ;;
    *)                     _t_pass "silent when Wi-Fi is not opted in" ;;
esac

# Credentials present: associate, then treat wlan0 as an interface to address.
WIFI_CONF_FIX="$WORK/wpa.conf"
printf 'network={\n  ssid="x"\n}\n' > "$WIFI_CONF_FIX"
out=$(wifi_call "$WIFI_CONF_FIX" "")
assert_contains "credentials start wpa_supplicant" "$(cat "$WIFI_TRACE")" "WPA_SUPPLICANT"
assert_contains "the supplicant is pointed at those credentials" \
    "$(cat "$WIFI_TRACE")" "$WIFI_CONF_FIX"
assert_contains "wlan0 joins the DHCP set" "$(cat "$NET_TRACE")" "UDHCPC -i wlan0"
assert_contains "the wired interface is still brought up too" "$(cat "$NET_TRACE")" "UDHCPC -i lo"
assert_contains "it says Wi-Fi was opted into" "$out" "Wi-Fi credentials present"

# Already associated: do not start a second supplicant, but still get a lease -
# a re-run must not leave wlan0 up with no address.
out=$(wifi_call "$WIFI_CONF_FIX" COMPLETED)
assert_empty "an associated radio is not re-associated" "$(cat "$WIFI_TRACE")"
assert_contains "an associated radio still gets DHCP" "$(cat "$NET_TRACE")" "UDHCPC -i wlan0"

# Credentials for a radio this board does not have must not start anything.
out=$(AD5X_WIFI_IFACE=no_such_radio wifi_call "$WIFI_CONF_FIX" "")
assert_empty "absent radio starts no supplicant" "$(cat "$WIFI_TRACE")"

# Step 2 must actually call it - a helper nothing invokes is the same bug.
plan=$(run_forced mips --dry-run)
assert_contains "the dry-run plan documents the network takeover" "$plan" "ifconfig up + udhcpc"
assert_contains "the dry-run plan documents the buzzer release" "$plan" "release the buzzer"

# Stopping the UI and taking the network over must be ONE operation. A version
# that kills firmwareExe and forgets the network is a printer nothing can reach,
# and that is not a hypothetical - it is what happened. Assert both halves fire
# from the single call the bring-up makes.
: > "$NET_TRACE"
out=$(AD5X_NET_IFACES=lo AD5X_NET_WAIT=1 NET_TRACE="$NET_TRACE" "$BASH_BIN" -c '
    . "$1"
    STOCK_UI_PROCS="firmwareExe"
    killall()  { echo "KILLALL $*" >> "$NET_TRACE"; }   # real call sends it to /dev/null
    ifconfig() { case "$2" in up) echo "IFCONFIG_UP $1";; esac; return 0; }
    ip()       { return 1; }
    udhcpc()   { echo "UDHCPC $*" >> "$NET_TRACE"; }
    cmd_pwm()  { echo "PWM $*" >> "$NET_TRACE"; }
    sleep()    { :; }
    stop_stock_ui
' _ "$BOOTSTRAP" 2>&1)
assert_contains "stop_stock_ui kills the stock UI" "$(cat "$NET_TRACE")" "KILLALL firmwareExe"
assert_contains "stop_stock_ui zeroes the buzzer duty cycle" "$(cat "$NET_TRACE")" "PWM set_wc pc12 0 0"
assert_contains "stop_stock_ui disables the buzzer channel" "$(cat "$NET_TRACE")" "PWM disable_channels pc12"
assert_contains "stop_stock_ui brings the interface up" "$out" "IFCONFIG_UP lo"
assert_contains "stop_stock_ui starts DHCP" "$(cat "$NET_TRACE")" "UDHCPC -i lo"

# ...and the bring-up must call THAT, not open-code half of it.
body=$(awk '/^run_bootstrap\(\)/,/^}/' "$BOOTSTRAP")
assert_contains "run_bootstrap calls stop_stock_ui" "$body" "stop_stock_ui"
case "$body" in
    *killall*) _t_fail "run_bootstrap does not open-code the killall" \
                       "killall appears directly in run_bootstrap, bypassing the network takeover" ;;
    *)         _t_pass "run_bootstrap does not open-code the killall" ;;
esac

# --- buzzer release ---------------------------------------------------------
#
# firmwareExe plays a melody at startup and drives the buzzer through
# `cmd_pwm set_level pc12 100`. Killing it mid-note leaves the PWM channel
# latched and the printer screams until it is power cycled - that happened on
# the rig, at level 1533857, and needed a manual disable_channels to stop.
: > "$NET_TRACE"
out=$(NET_TRACE="$NET_TRACE" AD5X_BUZZER_PWM=pc12 "$BASH_BIN" -c '
    . "$1"
    cmd_pwm() { echo "PWM $*" >> "$NET_TRACE"; }
    silence_stock_buzzer
' _ "$BOOTSTRAP" 2>&1)
assert_contains "buzzer duty is zeroed" "$(cat "$NET_TRACE")" "PWM set_wc pc12 0 0"
assert_contains "buzzer channel is disabled" "$(cat "$NET_TRACE")" "PWM disable_channels pc12"
assert_contains "the buzzer release is logged" "$out" "released pc12"

# The AD5M has no cmd_pwm; a missing tool must be a clean skip, never a failure
# that takes the whole bring-up down over a beep.
out=$(AD5X_BUZZER_CMD=definitely_not_a_command "$BASH_BIN" -c '
    . "$1"
    silence_stock_buzzer
' _ "$BOOTSTRAP" 2>&1)
_bz_rc=$?
assert_eq "a missing cmd_pwm is not fatal" "0" "$_bz_rc"
assert_contains "a missing cmd_pwm is reported" "$out" "not available"

rm -f "$NET_TRACE"

# --- progress bar -----------------------------------------------------------
#
# From step 2 the stock UI is dead and HEADLESS never repaints, so the panel
# freezes for the 2-3 minutes the rest of the bring-up takes. Every run then
# looks like a hang - and a user who power-cycles mid-bring-up leaves a PARTIAL
# klipper overlay (49 of 151 links, on the rig), which stock genuinely cannot
# boot from. The bar existing is what stops the printer teaching people to break
# it, so these assertions are about a safety property, not decoration.
PB=$(mktemp)
pb_call() {  # step, rows  -> paints into $PB
    : > "$PB"
    AD5X_FB="$PB" AD5X_FB_STRIDE=4 AD5X_FB_ROWS="$2" "$BASH_BIN" -c '
        . "$1"; progress "$2"
    ' _ "$BOOTSTRAP" "$1" >/dev/null 2>&1
}

pb_call 9 9
assert_eq "a full bar fills the framebuffer" "36" "$(wc -c < "$PB")"
assert_eq "a full bar is entirely lit" "36" "$(tr -dc '\377' < "$PB" | wc -c)"

pb_call 0 9
assert_eq "an empty bar still paints a full frame" "36" "$(wc -c < "$PB")"
assert_eq "an empty bar has nothing lit" "0" "$(tr -dc '\377' < "$PB" | wc -c)"

# Partial: step 3 of 9 = one third of the rows lit, and the lit rows come FIRST
# (it fills downward, so motion is visible).
pb_call 3 9
assert_eq "a third-way bar lights a third of the frame" "12" "$(tr -dc '\377' < "$PB" | wc -c)"
assert_eq "the lit rows are at the top" "12" "$(od -An -c "$PB" | tr -s ' ' '\n' | grep -c '^377$' || true)"
head -c 12 "$PB" | tr -dc '\377' | wc -c | grep -q '^12$' \
    && _t_pass "the bar fills downward from the top" \
    || _t_fail "the bar fills downward from the top" "leading bytes are not all lit"

# Escape hatch, same shape as the watchdog and the socket wait.
: > "$PB"
AD5X_PROGRESS=0 AD5X_FB="$PB" AD5X_FB_STRIDE=4 AD5X_FB_ROWS=9 "$BASH_BIN" -c '
    . "$1"; progress 5
' _ "$BOOTSTRAP" >/dev/null 2>&1
assert_eq "AD5X_PROGRESS=0 paints nothing" "0" "$(wc -c < "$PB")"

# A framebuffer that cannot be written must never take the bring-up down.
AD5X_FB=/definitely/not/a/device "$BASH_BIN" -c '
    . "$1"; progress 4
' _ "$BOOTSTRAP" >/dev/null 2>&1
assert_eq "a missing framebuffer is not fatal" "0" "$?"
rm -f "$PB"

# And the bring-up must actually paint - a bar nothing calls is the same bug as
# a network takeover nothing calls.
body=$(awk '/^run_bootstrap\(\)/,/^}/' "$BOOTSTRAP")
pb_calls=$(printf '%s\n' "$body" | grep -c '^ *progress [0-9]')
if [ "$pb_calls" -ge 8 ]; then
    _t_pass "run_bootstrap paints progress at every step ($pb_calls calls)"
else
    _t_fail "run_bootstrap paints progress at every step" "only $pb_calls progress calls"
fi

finish
