# Off-rig Klipper config-load validation

`ad5x_config_parse.sh` proves, entirely on a Linux desktop with no printer
attached, whether a Forge-X-shaped printer configuration reaches klippy's
config-load ("Ready") boundary on the target board. It is the failing test for a
config port: run it against a board's true-stock config and the mod's current
`.cfg` transplant, and it tells you every section klippy refuses to load and
exactly why, before you ever touch hardware.

## What it does

1. **Extract true-stock config** — pulls `printer.cfg` + `printer.base.cfg` and
   `klippy.tar` out of the board's factory image. Originals are never modified.
2. **Transplant** — runs the mod's `fix_config` HEADLESS batch (`cfg_backup.py`
   driven by `.cfg/*.cfg`), exactly as `.shell/init_lib.sh` does on-device,
   against disposable copies. The result is the `printer.cfg` /
   `printer.base.cfg` pair a real install would boot.
3. **Reproduce `/opt/config`** — inside an unprivileged user+mount namespace it
   tmpfs-mounts `/opt` and builds the on-device layout (`/opt/config/printer.cfg`,
   `mod` → the checkout, `mod_data/{user,variables}.cfg`). This is why absolute
   device paths baked into the config — e.g. `[mod_params] declaration:
   /opt/config/mod/mod_params.json` — resolve off-rig, with no root and no
   change to the real filesystem.
4. **Board-flavored klippy** — unpacks stock klippy, applies the mod's
   plugin/patch overlay (`.shell/klipper_overlay.sh`, `PLATFORM` forced via a
   shadowed `uname`), rebuilds the C helper with the host `gcc`, and loads the
   config pointed at a **nonexistent MCU serial**.

A `config.error` during load ⇒ **FAIL** (report the message). Reaching the
MCU-connect phase (`Unable to open serial port`) ⇒ **PASS**: config is
structurally valid; only real hardware could go further. Pin/hardware validation
happens at MCU connect and is deliberately out of scope here.

## Run it

```sh
# Needs the AD5X factory image (large, board-proprietary — not vendored).
tools/config-validate/ad5x_config_parse.sh --factory-tgz path/to/AD5X-...-Factory.tgz

# Or point at it once:
export AD5X_FACTORY_TGZ=path/to/AD5X-...-Factory.tgz
tools/config-validate/ad5x_config_parse.sh -v            # stream detail
tools/config-validate/ad5x_config_parse.sh --keep        # keep the temp workdir + klippy.log
tools/config-validate/ad5x_config_parse.sh --cfg-dir .cfg # default; point at an alternate .cfg set
```

Exit status: `0` PASS, `1` FAIL (config error), `2` harness/setup error.
The first run builds a small klippy venv under `~/.cache/ad5x-config-harness/`
(cffi, pyserial, greenlet, Jinja2, markupsafe, python-can); later runs reuse it.
Requires `python3`/`python3-venv`, `gcc`, `tar`, and unprivileged user
namespaces (`unshare --map-root-user --mount`).

## Platform hardware-macro overlay (package-time selection)

A handful of macro sections are AD5M-stock hardware that other boards do not
have. Rather than branch inside the shared macros, those sections live in
platform hardware-macro files that `base.cfg` / `headless.cfg` pull in:

| Include              | AD5M default (checked in) | AD5X override             |
|----------------------|---------------------------|---------------------------|
| `[include hw_base.cfg]`    | `macros/hw_base.cfg`      | `macros/hw_base.ad5x.cfg`      |
| `[include hw_display.cfg]` | `macros/hw_display.cfg`   | `macros/hw_display.ad5x.cfg`   |

`hw_base.cfg` holds the `[temperature_sensor weightValue]` load-cell sensor and
the `[gcode_macro G17/G18/G19]` arc-plane stubs; `hw_display.cfg` holds the
`[filament_switch_sensor e0_sensor]` runout switch. A plain checkout **is** the
AD5M build — those defaults load exactly as before. The AD5X overrides are
comment-only (the AD5X has no weight sensor, its stock `gcode_arcs.py` already
registers G17/G18/G19, and it uses the IFS instead of a klipper filament
switch).

**Selection is a file copy at package time.** To build a board image you copy
the board's override over the default before packaging:

```sh
cp macros/hw_base.ad5x.cfg    macros/hw_base.cfg
cp macros/hw_display.ad5x.cfg macros/hw_display.cfg
```

AD5M copies nothing (it is the default). This harness performs exactly that
copy in a **disposable mod-tree copy** (`$WORK/modtree`, symlinked to
`/opt/config/mod`) whenever `--platform` is not `ad5m`, so the real checkout is
never mutated. `stage_modtree()` is the reference implementation Stage B's
packaging step mirrors. Set `HELIX_HW_OVERLAY=0` to force the AD5M defaults on
any platform (used by the mutation check below).

## What it found (AD5M `.cfg` on AD5X true-stock), now behind the overlay

Before the platform hardware-macro overlay existed, the harness FAILED on three
ordered errors. Each is an AD5M-vs-AD5X **stock divergence**, not a transplant
bug, and each is now resolved by the AD5X override files above — `--platform
ad5x` reaches the MCU-connect boundary (PASS). Running with `HELIX_HW_OVERLAY=0`
forces the AD5M defaults back on and reproduces the first error, proving the
override is what makes it pass. klippy stops at the first error; each line below
is the next once the previous section is removed:

1. `Option 'sensor_type' in section 'temperature_sensor weightValue' must be
   specified` — the mod's `macros/base.cfg` ships a `[temperature_sensor
   weightValue]` load-cell/weight sensor (`trigger_value`/`throttle`/
   `exceed_gcode`, no `sensor_type`). The AD5X has no such sensor. **This is the
   sentinel error and it loads first.**
2. `gcode command G17 already registered` (then G18, then G19) — `macros/base.cfg`
   defines `[gcode_macro G17/G18/G19]` because the AD5M's older stock
   `gcode_arcs.py` did not register them. The AD5X's newer stock `gcode_arcs.py`
   registers G17/G18/G19 itself, so the macros collide.
3. `Option 'switch_pin' in section 'filament_switch_sensor e0_sensor' must be
   specified` — `macros/headless.cfg`'s `[filament_switch_sensor e0_sensor]`
   defines only `pause_on_runout` + `runout_gcode`. The AD5X stock
   `filament_switch_sensor.py` requires `switch_pin` (mainline behavior).

With those sections behind the AD5X override, klippy reaches the MCU-connect
boundary — the rest of the transplanted config (dual MCU, steppers, TMCs,
heaters, `[mod_params]` and the full mod plugin/macro tree, bed_mesh, probe,
`[lis2dw]`, `[skew_correction]`, `[exclude_object]`, …) loads clean on
AD5X-flavored klippy.

## Fidelity

**High — this is a real klippy object-load, not a text parse.** The errors above
are raised by klippy's `heaters`/`temperature_sensor`, `gcode`, and
`filament_switch_sensor` modules while instantiating objects, which a
configfile-only parse cannot catch. The harness uses the board's own stock
klippy plus the mod's exact overlay, the mod's real `cfg_backup.py` transplant,
and a faithful `/opt/config` so device-absolute paths resolve. Its one
off-rig substitution is the C-helper compiler (host `gcc` instead of the board
cross-compiler) — irrelevant to config validation. The only thing it cannot see
is what requires a live MCU dictionary: pin assignments and hardware limits,
which klippy checks at connect, after the boundary this tool stops at.
