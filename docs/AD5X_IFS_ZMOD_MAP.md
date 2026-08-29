# AD5X IFS: what zmod does, and what we have

ZMOD is the only working AD5X filament-system implementation, and it is the
reference for this port. This maps its surface onto ours so a gap is visible
rather than discovered on the printer.

Read it with `AD5X_IFS_PROTOCOL.md`, which covers the wire protocol itself.

Sources, all in ZMOD 1.7.1:

- `mod/_mod/.shell/zmod_ifs.py` - the klipper extra
- `mod/_mod/translate/en/ad5x.cfg`, `ad5x_display_off.cfg` - the macros

## The rules the board actually enforces

These cost several failed runs each, so they lead.

**A command is acknowledged before it is finished.** `F24 ok. chan 1.` says the
board took the opcode, not that it clamped. Send the next opcode into that
window and it answers `FFS not ready.`. zmod follows F24 and F39 with
`wait_for_state()`. Both halves are required: check the ack, then wait.

**Success is a return to READY, never the arrival of an activity state.**
`wait_for_state` returns `RET_OK` on `state == FFS_STATUS_READY`. The
per-channel activity (`CLAMPED`, `LOADING` + 11×(port-1)) is only the window in
which silk and stall are judged. Waiting for an activity to *arrive* times out.

**Refusals are prefixed like successes.** `F10 ok. FFS not ready.` is a refusal
and `F10 ok. FFS channel 1 feeding.` is a success. Only the payload separates
them, so every opcode must be checked against its expected reply.

**Poll at 0.2s while judging motion** (`HOST_REPORT_TIME`). The motion bit
toggles; sampling it at 1s reads a running motor as stalled.

**Counts:** `stall_count` 3, `silk_count` 1, `retry_count` 3.

## Opcodes

| Opcode | Meaning | Ours | State |
|---|---|---|---|
| F13 | status | `IfsOperations.poll_status` | done |
| F19 | capabilities | `parse_capabilities` | done |
| F24 | clamp | `clamp()` | done |
| F39 | release one | `release()` | done |
| F18 | release all | `release_all()` | done |
| F10 | feed | `feed()` | done |
| F11 | retract | `retract()` | done |
| F112 | stop | `stop()` | done |
| F23 | mark inserted | `mark_inserted()` | in ops, **no gcode command** |
| F15 | driver reset | `IFS_RESET_DRIVER` | done, **not wired to recovery** |

## zmod's python commands

| zmod | Purpose | Ours |
|---|---|---|
| `IFS_F10/F11/F13/F15/F18/F23/F24/F39/F112` | raw opcodes | `IFS_FEED/RETRACT/STATUS/RESET_DRIVER/RELEASE_ALL/-/CLAMP/RELEASE/STOP` |
| `IFS_STATUS` | report state | `IFS_STATUS` |
| `IFS_AUTOINSERT` | pull filament in at the IFS | **missing** |
| `IFS_EXTRUDER_SENSOR` | read the toolhead sensor | `IFS_SENSOR_VALUE` |
| `IFS_MOTION` | has it stopped / run out | **missing** |
| `INSERT_PRUTOK_IFS` | load to nozzle | `IFS_LOAD` |
| `REMOVE_PRUTOK_IFS` | unload from nozzle | `IFS_UNLOAD` |
| `PURGE_PRUTOK_IFS` | purge only | **missing** |
| `IFS_REMOVE_CURRENT_PRUTOK` | unload whatever is active | folded into `IFS_SELECT` |
| `SET_CURRENT_PRUTOK` / `SET_EXTRUDER_SLOT` | record the active lane | **missing** |
| `ANALOG_PRUTOK` | load an equivalent lane | out of scope |

## The flows

### Load - `_INSERT_PRUTOK_IFS` ("Loading filament IFS + Extruder")

```
_G28
IFS_REMOVE_CURRENT_PRUTOK          unload whatever is loaded
_GOTO_TRASH
M104 / TEMPERATURE_WAIT
IFS_F24 PRUTOK=n                   clamp, then settle
IFS_F10 LEN=1000 SPEED=1200 CHECK=1    IFS alone, to the toolhead sensor
_SBROS_TRASH_DAVIM PRUTOK=n        extruder AND IFS together, 90mm @ 300
_SBROS_TRASH / _CLEAR_REZINA       shake off / wipe
_SBROS_TRASH_DAVIM PRUTOK=0        purge again, extruder only
_SBROS_TRASH / _CLEAR_REZINA
IFS_F23 PRUTOK=n                   mark inserted
M106 S0 / M104 S0
SET_CURRENT_PRUTOK
```

Ours does: park, heat, clamp, feed, purge, wipe. **Gaps, in order of severity:**

1. **No co-push.** `_SBROS_TRASH_DAVIM` issues `G1 E90 F300` and then
   `IFS_F10 LEN=90 SPEED=300 SLEEP=1`. The `G1` is queued and returns at once,
   so gear and IFS drive the filament together. The IFS cannot push past a
   gripping extruder gear alone - observed as `stalled (channel 1 stopped
   moving)` with the filament held fast at the gear.
2. **No `_DISABLE_SENSOR` around extruder moves.** zmod brackets every
   `G1 E...` in a load or unload. Ours declares the toolhead sensor with
   `pause_on_runout: True`, so an extruder move can trip a runout pause.
3. **No F23** after a load, so the board is never told the lane is inserted.
4. **No unload-first step** - a load onto an occupied nozzle is not handled.
5. **No retry.** zmod retries a failed opcode up to `retry_count` (3), and
   resets the driver (F15) on `DRV_ERROR`.

### Unload - `_REMOVE_PRUTOK_IFS` ("Unloading filament Extruder + IFS")

```
_G28 / _GOTO_TRASH / heat
_CUT_PRUTOK                        cut the filament
_GOTO_TRASH / _CLEAR_REZINA
G1 E-<nozzle_cleaning_length>      retract, sensors disabled
```

**The AD5X has a filament cutter** (`_CUT_PRUTOK`, and a `cutValue` ADC beside
`filamentValue`). Ours models none of it. An unload that does not cut is not
this printer's unload.

### Purge - `_PURGE_PRUTOK_IFS`

`_SBROS_TRASH_DAVIM PRUTOK=0` then `_SBROS_TRASH` then `_CLEAR_REZINA`. Ours
folds purging into `IFS_LOAD` with no standalone command.

### Positions

| zmod | Ours |
|---|---|
| `_GOTO_TRASH` / `_GOTO_TRASH_STANDARD` | `_IFS_PARK_FOR_PURGE` via `_IFS_GOTO_STATION` |
| `_CLEAR_REZINA` | `_IFS_WIPE` |
| `_SBROS_TRASH` (shake off, no extrusion) | **missing** |
| `_MOVE_TO_CUT_PREPARE_POSITION` | **missing** (no cutter) |

## Deliberate deviations

Everything else should copy zmod. These do not, for stated reasons:

- **Payload checking lives in one place.** zmod repeats the expected reply at
  each call site; ours is `IfsOperations._expect`, so a command cannot forget.
- **The pre-command status is skipped by identity** (`_fresh_status`) rather
  than avoided by polling quickly. Stronger than a timing assumption.
- **`stall_state` is named for what it reports.** It carries MOTION, and zmod's
  name inverts that. Ours is `moving_channels` / `is_moving()`.
