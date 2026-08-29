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

**Only the load feed judges stalls.** zmod passes `CHECK=1` on exactly one call.
Everywhere else - notably the unload retract - it waits for READY and nothing
more, because motion stopping is how a retract *ends*.

**A co-driven feed is not watched at all.** Where the extruder drives the same
filament, zmod uses `SLEEP=1`: fire the opcode, pause `(len*20)//speed+1`, never
look at state. The lane's motion bit says nothing about a jam when something
else is pulling, and at 300 mm/min it reads as stopped within seconds.

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

1. ~~No co-push~~ - **done**. `_IFS_PURGE` issues `G1 E<n>` and then the lane's
   feed at the same length and speed; the `G1` is queued and returns, so both
   drive the filament together. The IFS cannot push past a gripping extruder
   gear alone.
2. ~~No `_DISABLE_SENSOR`~~ - **done**, as `_IFS_SENSOR_HOLD` /
   `_IFS_SENSOR_RESUME`. Save-and-restore, so a sensor the operator had off
   stays off. Klipper macros have no `finally`, so an error between the two
   still leaks the mute; `_IFS_PURGE` resumes on entry to heal that. zmod has
   the same hole.
3. ~~No F23~~ - **done**, `IFS_MARK_INSERTED`, and the load ends with it.
4. ~~No unload-first~~ - **done**, `_IFS_CLEAR_EXTRUDER`, which both the load and
   the unload begin with. A lane cannot be fed while another lane's filament
   occupies the combiner; the incoming filament simply pushes against it.
5. **No retry.** Still open, and the largest remaining gap. zmod retries a
   failed opcode up to `retry_count` (3), and calls F15 the moment the board
   reports `DRV_ERROR`. Ours fails the command instead, which is why a driver
   that dropped out ended the evening rather than being recovered from.

### Unload - `_REMOVE_PRUTOK_IFS` ("Unloading filament Extruder + IFS")

```
_G28 / _GOTO_TRASH / heat
_CUT_PRUTOK                        cut the filament
_GOTO_TRASH / _CLEAR_REZINA
G1 E-<nozzle_cleaning_length>      retract, sensors disabled
```

**The AD5X has a filament cutter** (`_CUT_PRUTOK`, and a `cutValue` ADC beside
`filamentValue`) - now modelled as `_IFS_CUT`, and verified on hardware: the
toolhead drives into a fixed blade at X -2.5, Y -7.5, Y first at F1800 then X at
F600, because the slow X move IS the cut. Reversing them would drag the head
across the front of the bed at the blade's depth.

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

## Drivers

The board carries two TMC drivers, reported by `IFS_DIAGNOSTICS` as `feeder` and
`selector`. Which is which was settled by watching the standstill bit: `F24`
drops standstill on the **selector**, so that is the motor positioning the IFS
to a lane, and the feeder is the one that moves filament.

Both can end up flagged `reset` - GSTAT bit 0, "driver was reset" - after a run
of stalls. In that state opcodes are accepted and motors are commanded, but no
fault is reported, so it can read like a mechanical jam. `IFS_DIAGNOSTICS` is
the only thing that distinguishes them, and it is worth reading BEFORE
concluding anything about filament.

`F15 C` clears the **feeder** flag, observed directly. It did not clear the
**selector** flag, and there is no selector reset in the opcode set - zmod has
none either.

What is NOT established: whether a flagged driver actually stops driving. The
test that looked like proof - feeding a second lane and seeing it stall too -
was invalid, because that lane's filament had been retracted clear out of the
IFS and there was nothing in the drive to move. A stall against an empty drive
proves nothing. Treat the `reset` flag as a signal worth reading, not as a
diagnosis, until something moves or fails to move with filament known to be
engaged.
