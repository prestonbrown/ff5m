#!/bin/bash

## Off-rig Klipper config-load validation harness.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license
##
## Proves, entirely on a Linux desktop with no printer attached, whether a
## Forge-X-shaped printer configuration reaches klippy's config-load ("Ready")
## boundary on the target board. It:
##
##   1. Extracts the board's TRUE-STOCK printer.cfg + printer.base.cfg from a
##      factory image (never mutating the originals).
##   2. Runs the mod's fix_config transplant (cfg_backup.py) against copies,
##      exactly as .shell/init_lib.sh fix_config() does on-device.
##   3. Reproduces the on-device /opt/config include tree (mod -> checkout,
##      mod_data/*) inside an unprivileged user+mount namespace, so absolute
##      device paths baked into the config (e.g. [mod_params] declaration)
##      resolve without root and without touching the real filesystem.
##   4. Loads the config with board-flavored klippy (stock klippy + the mod's
##      plugin/patch overlay) pointed at a nonexistent MCU serial.
##
## A config.error during load => FAIL (the config is not structurally valid on
## this board). Reaching the MCU-connect phase ("Unable to open serial port")
## => PASS (config loaded; only real hardware could go further).
##
## Everything happens in a self-cleaning temp dir. Nothing in the repo or the
## factory image is modified.

set -u

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
TOOL_DIR="$(dirname "$SELF")"
# tools/config-validate -> repo root (the mod source tree / "mod").
MOD_DIR="$(cd "$TOOL_DIR/../.." && pwd)"

CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/ad5x-config-harness"

# ---------------------------------------------------------------------------
# Defaults / arguments
# ---------------------------------------------------------------------------
PLATFORM="ad5x"        # klippy overlay flavor + uname the descriptor sees
DISPLAY_MODE="headless"
CFG_DIR=""             # mod .cfg param-move dir (default: $MOD_DIR/.cfg)
FACTORY_TGZ=""         # AD5X factory image; provides stock cfg + klippy
TIMEOUT=90
KEEP=0
VERBOSE=0

# Candidate factory-image locations searched when --factory-tgz is not given.
# The image is large and board-proprietary, so it is not vendored: supply it
# via --factory-tgz, $AD5X_FACTORY_TGZ, or by dropping it in fixtures/.
DEFAULT_TGZ_CANDIDATES=(
    "${AD5X_FACTORY_TGZ:-}"
    "$TOOL_DIR/fixtures"/*Factory*.tgz
    "$HOME"/Downloads/AD5X-*-Factory.tgz
)

usage() {
    cat <<EOF
Usage: $(basename "$SELF") [options]

Transplants a Forge-X config onto AD5X true-stock and loads it with
AD5X-flavored klippy, entirely off-rig. Prints PASS or FAIL: <klippy error>.

Options:
  --factory-tgz PATH   AD5X factory .tgz (source of stock cfg + klippy.tar).
                       Default: \$AD5X_FACTORY_TGZ, then tool fixtures/, then
                       the scratchpad factory dir, then ~/Downloads.
  --cfg-dir DIR        Mod .cfg param-move dir (default: \$MOD_DIR/.cfg).
  --platform NAME      Board flavor for the klipper overlay (default: ad5x).
  --display NAME       Display mode: headless|stock|feather|guppy (default: headless).
  --timeout SECS       Max seconds to let klippy run (default: 90).
  --keep               Keep the temp work dir (print its path).
  -v, --verbose        Stream klippy log + step detail.
  -h, --help           This help.

Exit status: 0 = PASS (reached MCU connect), 1 = FAIL (config error),
             2 = harness/setup error.
EOF
}

log()  { echo "[harness] $*" >&2; }
vlog() { [ "$VERBOSE" -eq 1 ] && echo "[harness] $*" >&2; return 0; }
die()  { echo "[harness] ERROR: $*" >&2; exit 2; }

# ---------------------------------------------------------------------------
# Namespace phase: reproduce /opt/config and run klippy. Re-entered via unshare.
# ---------------------------------------------------------------------------
run_in_namespace() {
    # Args (all passed by the outer phase): work dir, mod dir, venv, klippy,
    # config source dir, timeout, verbose.
    local work="$1" mod="$2" venv="$3" klippy_py="$4" stage="$5" timeout="$6" verbose="$7"

    # /opt becomes a private tmpfs so we can build /opt/config as root-in-ns
    # without touching the real /opt. The rest of the filesystem (the checkout,
    # the klippy tree, the venv) stays visible and unchanged.
    mount -t tmpfs tmpfs /opt || { echo "NS_ERR: tmpfs mount /opt failed" >&2; return 2; }
    mkdir -p /opt/config/mod_data || return 2

    cp "$stage/printer.cfg"      /opt/config/printer.cfg      || return 2
    cp "$stage/printer.base.cfg" /opt/config/printer.base.cfg || return 2

    # On-device layout: /opt/config/mod IS the checkout; mod_data holds the
    # user's variable/user configs. configparser tolerates a missing/empty
    # variables file, so empties are the faithful "fresh install" state.
    ln -s "$mod" /opt/config/mod        || return 2
    : > /opt/config/mod_data/user.cfg   || return 2
    : > /opt/config/mod_data/variables.cfg || return 2

    local logf="$work/klippy.log"
    : > "$logf"

    # Nonexistent serial: config load completes, then MCU connect fails to open
    # it (the PASS boundary). A PTY input tty and api socket keep klippy happy.
    "$venv/bin/python" "$klippy_py" /opt/config/printer.cfg \
        -l "$logf" -I "$work/ptty" -a "$work/api.sock" \
        >"$work/klippy.stdout" 2>&1 &
    local kpid=$!

    [ "$verbose" -eq 1 ] && tail -f "$logf" >&2 &
    local tailpid=$!

    local verdict="TIMEOUT" errmsg="" waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if ! kill -0 "$kpid" 2>/dev/null; then
            # klippy exited on its own (rare for load errors, which are caught).
            break
        fi
        if grep -q "Config error" "$logf" 2>/dev/null; then
            verdict="FAIL"; break
        fi
        if grep -q "Unhandled exception during connect" "$logf" 2>/dev/null; then
            verdict="FAIL"; break
        fi
        if grep -q "Unable to open serial port\|MCU error during connect\|Starting serial connect" "$logf" 2>/dev/null; then
            verdict="PASS"; break
        fi
        sleep 0.3
        waited=$((waited + 1))
    done

    kill "$kpid" 2>/dev/null
    wait "$kpid" 2>/dev/null
    kill "$tailpid" 2>/dev/null

    # Post-mortem scan in case the loop broke on process exit.
    if [ "$verdict" = "TIMEOUT" ]; then
        if grep -q "Config error\|Unhandled exception during connect" "$logf"; then
            verdict="FAIL"
        elif grep -q "Unable to open serial port\|MCU error during connect" "$logf"; then
            verdict="PASS"
        fi
    fi

    if [ "$verdict" = "FAIL" ]; then
        # The klippy exception traceback's terminal line carries the real
        # message: "<module>.error: <text>" or "<Exception>: <text>".
        errmsg="$(awk '
            /Config error|Unhandled exception during connect/ {cap=1}
            cap && /^[A-Za-z_][A-Za-z0-9_.]*(error|Error|Exception):/ {last=$0}
            END {print last}
        ' "$logf")"
        [ -z "$errmsg" ] && errmsg="$(grep -E "^[A-Za-z_].*(error|Error|Exception):" "$logf" | tail -1)"
    fi

    printf '%s\t%s\n' "$verdict" "$errmsg" > "$work/verdict.tsv"
    return 0
}

# ---------------------------------------------------------------------------
# Outer phase: setup on the real filesystem, then re-exec into the namespace.
# ---------------------------------------------------------------------------

extract_stock() {
    local tgz="$1" out="$2"
    mkdir -p "$out/x"
    log "Extracting true-stock config + klippy from $(basename "$tgz")"
    tar xf "$tgz" -C "$out/x" ./other/printer.cfg ./other/klippy.tar \
        || die "cannot read printer.cfg/klippy.tar from factory image"
    # printer.base.cfg lives inside the software payload.
    local swtar
    swtar="$(tar tf "$tgz" | grep -E '^\./software-.*\.tar\.xz$' | head -1)"
    [ -n "$swtar" ] || die "no software-*.tar.xz in factory image"
    tar xf "$tgz" -C "$out/x" "$swtar" || die "cannot extract software payload"
    tar xf "$out/x/$swtar" -C "$out/x" ./printer.base.cfg \
        || die "no printer.base.cfg in software payload"

    mkdir -p "$out/stage"
    cp "$out/x/other/printer.cfg" "$out/stage/printer.cfg"
    cp "$out/x/printer.base.cfg"  "$out/stage/printer.base.cfg"
    cp "$out/x/other/klippy.tar"  "$out/klippy.tar"
}

transplant() {
    # Replicates .shell/init_lib.sh fix_config() (HEADLESS batch) against the
    # staged copies. Paths here are host paths (no chroot); the on-device
    # /opt/config/mod/.cfg prefix maps to $CFG_DIR.
    local stage="$1" cfgdir="$2" work="$3"
    local pcfg="$stage/printer.cfg" pbase="$stage/printer.base.cfg"
    local py="$MOD_DIR/.py/cfg_backup.py"
    local move_data="$work/move.data.cfg"

    # Mirror .shell/init_lib.sh fix_config's _cfg_path: prefer a platform override
    # (.cfg.$PLATFORM/<file>) when it exists, else the shared $cfgdir/<file>. The
    # override dir is a sibling of .cfg under the mod tree; ad5m has none, so every
    # name falls back to the shared file and the AD5M batch stays byte-identical.
    _cfg_path() {
        if [ -f "$MOD_DIR/.cfg.$PLATFORM/$1" ]; then
            echo "$MOD_DIR/.cfg.$PLATFORM/$1"
        else
            echo "$cfgdir/$1"
        fi
    }

    log "Transplanting (fix_config HEADLESS batch) with cfg_backup.py"

    # 1. Back up [heater_bed] out of printer.base.cfg; fall back to the shipped
    #    sample if the extract fails, exactly as fix_config does.
    if python3 "$py" --mode backup --config "$pbase" \
            --data "$move_data" --params "$(_cfg_path init.move.cfg)" >/dev/null 2>&1; then
        vlog "  step1 backup heater_bed -> $move_data"
    else
        move_data="$(_cfg_path data.init.move.cfg)"
        vlog "  step1 backup failed; using fallback $move_data"
    fi

    # Display-mode param file (step 3).
    local disp="$cfgdir/init.display.$DISPLAY_MODE.cfg"
    [ -f "$disp" ] || die "no display param file for '$DISPLAY_MODE': $disp"

    # Resolve each transplant fragment through the platform override, exactly as
    # fix_config's $(_cfg_path ...) does on-device. Display + tuning carry no
    # override, so they stay on the shared $cfgdir.
    local p_move p_datainit p_initcfg p_database p_initbase
    p_move="$(_cfg_path init.move.cfg)"
    p_datainit="$(_cfg_path data.init.cfg)"
    p_initcfg="$(_cfg_path init.cfg)"
    p_database="$(_cfg_path data.init.base.cfg)"
    p_initbase="$(_cfg_path init.base.cfg)"

    local batch="$work/batch.json"
    cat > "$batch" <<JSON
[
  {"mode":"restore","config":"$pcfg","data":"$move_data","params":"$p_move","avoid_writes":true},
  {"mode":"restore","config":"$pcfg","params":"$disp","no_data":true,"avoid_writes":true},
  {"mode":"restore","config":"$pcfg","data":"$p_datainit","params":"$p_initcfg","avoid_writes":true},
  {"mode":"restore","config":"$pbase","data":"$p_database","params":"$p_initbase","avoid_writes":true},
  {"mode":"restore","config":"$pcfg","params":"$cfgdir/tuning.off.cfg","no_data":true,"avoid_writes":true}
]
JSON

    local out
    if [ "$VERBOSE" -eq 1 ]; then
        python3 "$py" --batch "$batch" >&2 || die "cfg_backup.py batch failed"
    else
        out="$(python3 "$py" --batch "$batch" 2>&1)" || { echo "$out" >&2; die "cfg_backup.py batch failed"; }
    fi
    vlog "  transplant done"
}

ensure_venv() {
    local venv="$CACHE_DIR/venv"
    if [ -x "$venv/bin/python" ] && "$venv/bin/python" - <<'PY' >/dev/null 2>&1
import cffi, serial, greenlet, jinja2, markupsafe
PY
    then
        echo "$venv"; return 0
    fi
    log "Building klippy venv (one-time) at $venv"
    mkdir -p "$CACHE_DIR"
    python3 -m venv "$venv" || die "python3 -m venv failed (need python3-venv)"
    "$venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1
    # python-can is only needed for canbus MCUs; install for completeness.
    "$venv/bin/pip" install --quiet \
        cffi pyserial greenlet Jinja2 markupsafe python-can \
        || die "pip install of klippy deps failed"
    echo "$venv"
}

overlay_klippy() {
    local work="$1"
    local ktree="$work/klipper"
    mkdir -p "$ktree"
    log "Unpacking stock klippy + applying mod overlay (PLATFORM=$PLATFORM)"
    tar xf "$work/klippy.tar" -C "$ktree" || die "cannot unpack klippy.tar"
    # Force a host rebuild of the C helper (shipped .so is the board's arch).
    rm -f "$ktree/klippy/chelper/c_helper.so"
    # The board's chelper is hardwired to its cross-compiler (e.g.
    # mips-linux-gnu-gcc on the AD5X). Off-rig we build with the host gcc.
    local che="$ktree/klippy/chelper/__init__.py"
    sed -i -E 's/^GCC_CMD = "[^"]*"/GCC_CMD = "gcc"/' "$che"
    sed -i -E 's/^HC_COMPILE_CMD = "[^ ]+/HC_COMPILE_CMD = "gcc/' "$che"

    # The descriptor picks the board by `uname -m`; shadow uname so the overlay
    # applies the AD5X patch tree + .exclude on this x86 host (the documented
    # test mechanism in .shell/platform.sh).
    local fakebin="$work/fakebin" machine
    mkdir -p "$fakebin"
    case "$PLATFORM" in
        ad5x) machine="mips" ;;
        ad5m) machine="armv7l" ;;
        *)    machine="$(uname -m)" ;;
    esac
    cat > "$fakebin/uname" <<EOF
#!/bin/sh
if [ "\$1" = "-m" ]; then echo "$machine"; else exec /usr/bin/uname "\$@"; fi
EOF
    chmod +x "$fakebin/uname"

    # apply_klipper_patches links plugins + patches over the stock tree.
    # KLIPPER_TUNE_CMD=true replaces the on-device toolhead tweak (irrelevant
    # to config load). KLIPPER_TARGET_DIR points at our unpacked tree.
    (
        export PATH="$fakebin:$PATH"
        export KLIPPER_SRC_DIR="$MOD_DIR/.py/klipper"
        export KLIPPER_TARGET_DIR="$ktree/klippy"
        export KLIPPER_TUNE_CMD="true"
        # shellcheck disable=SC1090
        . "$MOD_DIR/.shell/klipper_overlay.sh"
        if [ "$VERBOSE" -eq 1 ]; then
            apply_klipper_patches >&2
        else
            apply_klipper_patches >/dev/null 2>&1
        fi
    ) || die "klipper overlay failed"

    echo "$ktree/klippy/klippy.py"
}

resolve_factory_tgz() {
    if [ -n "$FACTORY_TGZ" ]; then
        [ -f "$FACTORY_TGZ" ] || die "--factory-tgz not found: $FACTORY_TGZ"
        echo "$FACTORY_TGZ"; return 0
    fi
    local c
    for c in "${DEFAULT_TGZ_CANDIDATES[@]}"; do
        [ -n "$c" ] && [ -f "$c" ] && { echo "$c"; return 0; }
    done
    die "no factory image found; pass --factory-tgz PATH (AD5X-*-Factory.tgz)"
}

stage_modtree() {
    # Build a disposable copy of the mod tree that /opt/config/mod points at, so
    # the platform overlay never mutates the real checkout. This is where the
    # package-time platform selection happens: the mod ships AD5M-default
    # hardware-macro files (macros/hw_base.cfg, macros/hw_display.cfg) plus a
    # per-board override alongside each (macros/hw_base.<platform>.cfg,
    # macros/hw_display.<platform>.cfg). Selecting a board copies its override
    # over the default; AD5M is the default and copies nothing. Stage B's
    # packaging step performs this same copy when it assembles a board image.
    #
    # FORGEX_HW_OVERLAY=0 forces the AD5M defaults on any platform (used by the
    # mutation check, which proves the override is what greens AD5X).
    local work="$1"
    local modtree="$work/modtree"
    mkdir -p "$modtree"
    cp -a "$MOD_DIR/." "$modtree/" || die "cannot stage mod tree copy"

    if [ "$PLATFORM" = "ad5m" ] || [ "${FORGEX_HW_OVERLAY:-1}" = "0" ]; then
        vlog "  platform overlay: none (AD5M defaults for platform=$PLATFORM)"
        echo "$modtree"; return 0
    fi

    local m="$modtree/macros" hb="hw_base.$PLATFORM.cfg" hd="hw_display.$PLATFORM.cfg"
    [ -f "$m/$hb" ] || die "missing macros/$hb for platform $PLATFORM"
    [ -f "$m/$hd" ] || die "missing macros/$hd for platform $PLATFORM"
    cp "$m/$hb" "$m/hw_base.cfg"    || die "hw_base overlay failed"
    cp "$m/$hd" "$m/hw_display.cfg" || die "hw_display overlay failed"
    vlog "  platform overlay: $hb -> hw_base.cfg, $hd -> hw_display.cfg"

    echo "$modtree"
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --factory-tgz) FACTORY_TGZ="$2"; shift 2 ;;
            --cfg-dir)     CFG_DIR="$2"; shift 2 ;;
            --platform)    PLATFORM="$2"; shift 2 ;;
            --display)     DISPLAY_MODE="$2"; shift 2 ;;
            --timeout)     TIMEOUT="$2"; shift 2 ;;
            --keep)        KEEP=1; shift ;;
            -v|--verbose)  VERBOSE=1; shift ;;
            -h|--help)     usage; exit 0 ;;
            # Internal: namespace re-entry.
            --_run-ns)     shift; run_in_namespace "$@"; exit $? ;;
            *) die "unknown argument: $1" ;;
        esac
    done

    [ -n "$CFG_DIR" ] || CFG_DIR="$MOD_DIR/.cfg"
    [ -d "$CFG_DIR" ] || die "cfg dir not found: $CFG_DIR"
    command -v unshare >/dev/null || die "unshare not available"
    command -v tar >/dev/null || die "tar not available"

    local tgz; tgz="$(resolve_factory_tgz)" || exit 2
    local venv; venv="$(ensure_venv)" || exit 2

    local WORK; WORK="$(mktemp -d "${TMPDIR:-/tmp}/ad5x-cfgparse.XXXXXX")"
    if [ "$KEEP" -eq 1 ]; then
        log "work dir (kept): $WORK"
    else
        trap 'rm -rf "$WORK"' EXIT
    fi

    log "mod tree:  $MOD_DIR"
    log "cfg dir:   $CFG_DIR"
    log "platform:  $PLATFORM   display: $DISPLAY_MODE"

    extract_stock "$tgz" "$WORK"
    transplant "$WORK/stage" "$CFG_DIR" "$WORK"

    # Deprecated-option gate. klippy surfaces these only as runtime configfile
    # status warnings (what Fluidd lists), never in klippy.log, so the gate
    # reads the transplanted product itself. AD5X-only: the AD5M's older
    # klippy requires max_accel_to_decel, so its overlay must keep it. Extend
    # the list as newer stock deprecations surface.
    if [ "$PLATFORM" = "ad5x" ]; then
        local option
        for option in max_accel_to_decel; do
            if grep -qE "^[[:space:]]*${option}[[:space:]]*:" "$WORK/stage/printer.base.cfg"; then
                echo
                echo "RESULT: FAIL  deprecated option survived the transplant:"
                echo "        $option in printer.base.cfg (.cfg.$PLATFORM/init.base.cfg should remove it)"
                exit 1
            fi
        done
    fi

    local klippy_py; klippy_py="$(overlay_klippy "$WORK")" || exit 2
    # Disposable mod-tree copy with the platform hardware-macro overlay applied.
    local modtree; modtree="$(stage_modtree "$WORK")" || exit 2

    log "Loading config with klippy (nonexistent MCU serial; ${TIMEOUT}s cap)"
    # Re-enter as fake-root in a fresh mount namespace to build /opt/config.
    # /opt/config/mod is symlinked to the staged copy (not the real checkout).
    unshare --map-root-user --mount --uts -- \
        "$SELF" --_run-ns "$WORK" "$modtree" "$venv" "$klippy_py" \
        "$WORK/stage" "$TIMEOUT" "$VERBOSE" \
        || die "namespace phase failed to run"

    [ -f "$WORK/verdict.tsv" ] || die "no verdict produced (see $WORK/klippy.log)"
    local verdict errmsg
    verdict="$(cut -f1 "$WORK/verdict.tsv")"
    errmsg="$(cut -f2- "$WORK/verdict.tsv")"

    echo
    case "$verdict" in
        PASS)
            echo "RESULT: PASS  (config loaded; reached MCU-connect boundary)"
            echo "        The transplanted config is structurally valid on $PLATFORM."
            [ "$KEEP" -eq 1 ] && echo "        log: $WORK/klippy.log"
            exit 0 ;;
        FAIL)
            echo "RESULT: FAIL  config error:"
            echo "        $errmsg"
            [ "$KEEP" -eq 1 ] && echo "        log: $WORK/klippy.log"
            exit 1 ;;
        *)
            echo "RESULT: UNDETERMINED  (klippy neither errored nor reached MCU connect within ${TIMEOUT}s)"
            echo "        Inspect: $WORK/klippy.log"
            KEEP=1; trap - EXIT
            exit 2 ;;
    esac
}

main "$@"
