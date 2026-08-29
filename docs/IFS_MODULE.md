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

## Notes

- `IFS_SET_MATERIAL` colours are bare hex (`7EC8E3`, `F80`) or `#`-prefixed
  when called from a place `#` survives. Klipper's parser starts a comment
  at `#`, so `COLOR=#7EC8E3` arrives empty and the quoted form is rejected
  outright.
- UNLOAD takes filament out of the nozzle and leaves the lane threaded;
  EJECT pulls it out of the machine entirely. A tool change is a load, and
  it unloads the outgoing lane itself - slicers only need `T0`..`T3`.
