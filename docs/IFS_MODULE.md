# The AD5X IFS module

Support for the AD5X's 4-channel filament system (IFS) as a drop-in klipper
module: Python plugins plus one gcode file. It runs on any klipper host -
a stock-style image, zmod, Forge-X, or a base build of your own - and uses
the host's conveniences only when they are present.

## What it provides

- The whole tool-change choreography as readable, console-runnable macros:
  `IFS_SELECT`, `IFS_LOAD`, `IFS_UNLOAD`, `IFS_EJECT`, `IFS_PURGE`,
  `IFS_MOTION`, `IFS_AUTOINSERT`, and `T0`..`T3` mapped to slots 1-4 the way
  a slicer writes them.
- `IFS_STATUS`, `IFS_MATERIALS`, `IFS_DIAGNOSTICS` for asking the board and
  the slot registry what is true right now.
- `IFS_REINIT_DRIVERS` to re-initialise both TMC drivers. This is the only
  recovery for a **selector** driver carrying GSTAT's reset flag -
  `IFS_RESET_DRIVER` (F15 C) is not a driver operation at all; it drops the
  feeder's enable line and writes no TMC register. Refused unless the board is
  idle, and it puts the run current back to the board's stock IRUN 9.
- `IFS_JOG_SELECTOR POSITION=<steps>` - **diagnostic**, and the sharpest thing
  here. It drives the selector to an absolute position without the re-home
  every clamp performs, which is how the selector's step rate gets measured,
  but it bypasses the board's sequencing, leaves `chan` reporting the previous
  lane, and would strand the board in state 129 if the command did not free it
  on the way out. Not part of any sequence. See
  [the protocol doc](AD5X_IFS_PROTOCOL.md) before using it.
- `IFS_MAP_TOOL TOOL=1 SLOT=4` to aim a slicer tool at a different lane for
  the current print, `IFS_MAP_TOOL RESET=1` to put the identity back. See
  [Tool remapping](#tool-remapping).
- `IFS_SET_MATERIAL SLOT=2 TYPE=PLA COLOR=7EC8E3` to register what a slot
  holds. Colours drive a purge that scales with how far apart the outgoing
  and incoming colours are; types drive handling temperatures and the
  material-change flush bonus.
- A colour-distance purge: the first pass is scaled between a 50 mm floor
  and your printer's own full setting by RGB distance between the two
  slots' registered colours. Unknown colours, and material type changes,
  always get the full length (plus the type bonus).

## Requirements

1. **Klipper with pyserial** available to klippy (it is on every image that
   runs Moonraker).
2. **A UART to the IFS board.** The IFS is a separate microcontroller; the
   module talks to it directly at 115200 8N1. Default port `/dev/ttyS4` -
   override in the `[ifs]` section.
3. **The toolhead filament sensor's ADC.** The sensor is an analog input on
   the extruder board. Declare it so klipper samples it (a thermistor
   section is the trick every FlashForge image uses), and name it in the
   sensor section:

       [temperature_sensor filamentValue]
       sensor_type: Generic 3950
       sensor_pin: eboard:PA3

4. **`[save_variables]`**, which the module declares itself - check that the
   path it names is a writable, persistent location on your image.
5. **`[pause_resume]`** if you want jam detection to be able to pause a
   print (any Moonraker host already has it).

## Install

1. Copy the plugin files into your klippy's `extras/` directory:

       ifs.py  ifs_link.py  ifs_status.py  ifs_operations.py  ifs_sequences.py
       ifs_materials.py  flashforge_config.py
       ifs_sensor_base.py  ifs_sensor_logic.py
       ifs_toolhead_sensor.py  ifs_channel_sensor.py  ifs_diagnostics.py
       ifs_jacker.py  (optional - the IFS Jacker companion, see below)

   (all from `.py/klipper/plugins/` in this repository). Nothing else is
   needed - klipper imports an extra when a config section names it.

2. Copy `macros/ifs.cfg` somewhere your printer config can reach and add:

       [include ifs.cfg]

3. Restart klippy. `IFS_STATUS` should report the board's firmware version.

4. Register what is in each slot (see `IFS_SET_MATERIAL` above). Slot
   registration lives in FlashForge's own settings file when one exists, so
   a stock UI that writes it stays in agreement; on an image without that
   file the module's registry is simply always right.

## Host hooks, all optional

The module asks whether each of these exists and degrades gracefully when
it does not:

| Hook | Used for | Without it |
|------|----------|------------|
| `MOVE_SAFE` macro | the tool-change entry lift (it clamps to axis bounds) | the same relative lift via `SAVE_GCODE_STATE`/`G91` |
| a `fan_generic` named in `_IFS_GEOMETRY.part_fan` | the part fan (purge-tail freeze, print fan restore) | stock `M106` |
| `_CLIENT_VARIABLE` with `custom_park_x/y` | aiming the end-of-print park at the purge chute | your host's own end behavior |

## The shared filament verbs

A host that already defines `LOAD_FILAMENT` / `UNLOAD_FILAMENT` /
`PURGE_FILAMENT` - this repository's shared macros do, as bare extruder moves -
can point them at the module instead: rename each verb and forward to
`IFS_LOAD` / `IFS_UNLOAD` / `IFS_PURGE`. That is what
`macros/hw_base.ad5x.cfg` does on the AD5X, so `M600`'s change prompt,
`LOAD_MATERIAL`'s action menu and slicer stop-gcode all drive the lanes
without naming an IFS command. `SLOT=` passes through and otherwise the loaded
lane is used; `SPEED=` is accepted and ignored, because lane speed is the
board's own setting; `MATERIAL=` registers the slot's type first
(`IFS_SET_MATERIAL`'s `TYPE=`).

## Tool remapping

A multi-material file says `T0`..`T3`; the tool map says which lane each of
those loads. It is the identity - `T0`->1 .. `T3`->4, exactly the numbering
above - until `IFS_MAP_TOOL TOOL=1 SLOT=4` rewrites one entry. A mapping is
refused for a lane with nothing in it, so a tool cannot be aimed at a lane
that cannot fulfill it. `IFS_MAP_TOOL RESET=1` restores the identity.

The map is per-print state and nothing more: it is not persisted, it is
cleared when the print ends, and a klippy restart loses it. A client that
cares (a UI offering to match the file's colours to what is loaded) sends
its `IFS_MAP_TOOL` lines before the print starts and re-sends them after a
restart. `IFS_SELECT SLOT=n` from the console or a host verb does not go
through the map - a hand that names a lane means that lane.

The active map is echoed live in `printer.ifs.tool_map`, subscribable like
every other status field. Its shape, after JSON serialization:

    "tool_map": {"0": 1, "1": 2, "2": 3, "3": 4}

Keys are tool numbers as strings (`"0"` is `T0`), values are the 1-based
lane numbers `IFS_LOAD SLOT=` takes.

## Tuning

All station geometry is `[gcode_macro _IFS_GEOMETRY]` variables - chute,
wiper, safe-Y, change lift, how far an inserted lane may pre-thread. Retune
from the console without touching a motion macro:

    SET_GCODE_VARIABLE MACRO=_IFS_GEOMETRY VARIABLE=chute_x VALUE=53.0

Handling temperatures per material are `[ifs_materials]` options
(`temp_PETG: 240`), and the load/purge/unload distances come from the
printer's own `Multicolour` settings block when one exists.

The wire protocol the board speaks, for anyone debugging at that level:
[AD5X_IFS_PROTOCOL.md](AD5X_IFS_PROTOCOL.md).

## Companion: the IFS Jacker

The IFS Jacker (https://github.com/ninjamida/ifs-jacker) is a pass-through that
sits between the host and the IFS board: F-opcode traffic goes through
untouched, and the Jacker answers Z opcodes of its own. A bare board answers a
Z command with silence, so detection is a capped probe - every silent attempt
chased by an `F13`, which both proves the link alive and clears the command the
board could not answer - and a machine without a Jacker is left alone after
three silent probes.

Copy `macros/ifs_jacker.cfg` next to `ifs.cfg` and include it only on a machine
that has the device; it requires `[ifs]`. Two options:

| `[ifs_jacker]` option | Default | What it is |
|---|---|---|
| `probe_delay` | 30 | seconds after startup before the first detection probe, so the IFS connects first |
| `probe_attempts` | 3 | silent probes before giving up until the link is re-established |

`IFSJ_CHECK` asks again and reports what it found (version, channel count,
peripheral count). `IFSJ_Z1` to `IFSJ_Z5` send the companion's own commands -
Z3 and Z4 need firmware 3.0, Z5 needs 2.2, and the plugin reports the
requirement rather than guessing. Firmware 3.0 also brought peripherals: their
state arrives appended to every status line as `p<id>_<param>` tuples,
published under `peripherals` in the section's status object alongside
`detected`, `version` and the counts.

## Notes

- `IFS_SET_MATERIAL` colours are bare hex (`7EC8E3`, `F80`) or `#`-prefixed
  when called from a place `#` survives. Klipper's parser starts a comment
  at `#`, so `COLOR=#7EC8E3` arrives empty and the quoted form is rejected
  outright.
- UNLOAD takes filament out of the nozzle and leaves the lane threaded;
  EJECT pulls it out of the machine entirely. A tool change is a load, and
  it unloads the outgoing lane itself - slicers only need `T0`..`T3`.
