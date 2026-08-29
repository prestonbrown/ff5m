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
| F15 | driver reset | `IFS_RESET_DRIVER` | done, and wired to the retry |

## zmod's python commands

| zmod | Purpose | Ours |
|---|---|---|
| `IFS_F10/F11/F13/F15/F18/F23/F24/F39/F112` | raw opcodes | `IFS_FEED/RETRACT/STATUS/RESET_DRIVER/RELEASE_ALL/-/CLAMP/RELEASE/STOP` |
| `IFS_STATUS` | report state | `IFS_STATUS` |
| `IFS_AUTOINSERT` | pull filament in at the IFS | `IFS_AUTOINSERT`, fired on the board's insert report |
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
4. ~~No unload-first~~ - **done**. zmod's load opens with
   `IFS_REMOVE_CURRENT_PRUTOK`, which runs a full `IFS_REMOVE_PRUTOK` and pulls
   the old strand back into its own lane. Ours does the same now that the loaded
   lane is on record: `IFS_LOAD` calls `IFS_UNLOAD` for it, and falls back to
   `_IFS_CLEAR_EXTRUDER` only when no lane is recorded and there is nothing to
   retract. Clearing just the extruder left the old filament at the hub, which
   is where the incoming lane arrives.

   **There is no shared bowden on an AD5X.** Each lane runs its own tube the
   whole way and the hub is mounted ON the toolhead, feeding the extruder a few
   centimetres later, so the hub and the extruder are the only shared parts of
   the path. `tube_length` is one lane's full run, not a hub-to-toolhead
   segment.
5. ~~No retry~~ - **done**. Every move re-issues its opcode up to
   `retry_count` (3) and sends F15 on each `DRV_ERROR`, as zmod's
   `wait_for_state` + `cmd_IFS_F10` pair does. A stall is not retried: zmod
   breaks on `RET_STALL`, and driving into a jam again grinds a flat on the
   filament.

6. ~~No auto-insert~~ - **done**, and this was the biggest one. zmod runs
   `_IFS_AUTOINSERT` the moment the board reports filament pushed into a lane:
   it draws that lane up to the toolhead sensor and backs off 90 mm. Our insert
   event was detected and then dropped on the floor, so no lane was ever in a
   position anything downstream could assume.

7. ~~No lane tracking~~ - **done**. `ifs.active_channel` is the board's
   *selector* position, not what is in the nozzle, and it reads 0 after a power
   cycle with filament still loaded, so `IFS_SELECT` would have loaded a second
   lane on top of the first. zmod reads the loaded lane out of FlashForge's own
   config (`FFMInfo.channel`); Forge-X has no stock config to read, so it is
   recorded in `save_variables` - the load writes it, the unload clears it, and
   both `IFS_UNLOAD` and `IFS_SELECT` read it.

**Still open:** `IFS_MOTION` (jam vs runout for the loaded lane, and what its
`_PRINT_IFS_MOTION PAUSE=` argument actually drives), a standalone
`PURGE_PRUTOK_IFS`, and `_SBROS_TRASH`'s shake-off move.

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
- **A move aimed at a sensor fails if it never reaches it.** zmod's checked
  feed returns `RET_OK` when the board simply finishes, and its macro carries
  on regardless; ours reports `not_reached`. Asking to feed UNTIL the toolhead
  and finishing with the sensor still empty is not a success, and treating it
  as one let a load purge filament that was still in the tube.
- **Auto-insert can be switched off** (`autoinsert:` in `[ifs]`, default on).
  zmod always threads an inserted lane. A printer that moves filament on its
  own the moment somebody touches it is worth being able to stop.
- **Every newly inserted lane is threaded**, not just the highest-numbered one.
  zmod collapses the insert mask with `bit_length()` and loses the rest.
- **The first insert reading only primes.** The board reports insert as a
  level, so a fresh watcher's first non-zero reading is not an edge - it is
  whatever was already in the lanes. zmod's `Insert` starts at 0 and fires on
  it, which would mean driving three lanes at once on every klippy restart.
- **`stall_state` is named for what it reports.** It carries MOTION, and zmod's
  name inverts that. Ours is `moving_channels` / `is_moving()`.

## Measured: feed and retract are not symmetric

Sampled from the board at 0.2 s during cold, isolated moves, with the plugin in
its move cadence. The board stays in its activity state for the whole commanded
duration whatever happens, so the *motion bit* is the only thing that says
whether filament actually moved.

| move | commanded | motion |
|---|---|---|
| feed lane 1 | 200 mm / 10.0 s | 5.5 s, then nothing |
| retract lane 1 | 200 mm / 10.0 s | **10.3 s, continuous** |
| feed lane 1 | 300 mm / 15.0 s | 11.5 s |
| feed lane 1 | 400 mm / 20.0 s | 2.0 s |
| retract lane 4 | 150 mm / 7.5 s | **7.8 s, continuous** |
| feed lane 2 | 150 mm / 7.5 s | 5.0 s |
| lane 1 via `IFS_AUTOINSERT` | 600 mm / 30.0 s | 1.3 s |

Every retract runs its full length. Every feed dies early, on every lane tried,
and repeated feeds on one lane get *worse* - which is what grinding a flat on
the filament looks like. Two different lanes behaving the same way rules out one
lane's tip, and the drive, the driver and the motion sensor are all evidently
working because the retract uses all three.

### The distances look like slack, not an obstruction

Read in order, the feed distances track what had just been pushed back up the
tube rather than any fixed point:

- feed 1 moved 110 mm and stopped
- a 200 mm retract then ran its full length
- feed 2 moved 230 mm - roughly the 200 mm the retract had just returned
- feed 3, immediately after, managed 40 mm
- and the auto-insert, later still, managed 25 mm

A fixed obstruction does not move when you retract, and it does not grant you
exactly as much travel as you just gave back. Slack does.

The asymmetry says the same thing. Feeding has to **draw filament off the
spool**; retracting only has to push slack back toward it, which needs nothing
of the spool at all. So a lane whose spool will not pay out feeds until the
slack between spool and drive is used up, then stalls, and retracts perfectly
every time. Lanes 1, 2 and 4 all behave this way, which points at something
common to the spool side rather than at three separate jams.

This is a hypothesis with a one-minute physical test: pull filament off each
spool by hand at the IFS input. It has not been run.

The one thing the numbers say without ambiguity is **stop feeding**. Each
attempt on lane 1 moved less than the one before, which is what grinding a flat
on the filament at the drive gear looks like, and every retry makes recovery
harder. The board's own reports have ruled the software out; this needs hands.

`IFS_AUTOINSERT` has since been run on lane 1 and behaved exactly as intended:
clamp, feed, stall detected at three consecutive still polls, F112, a clean
`gcode.error` naming the channel, and no klippy shutdown. It found the same
mechanical wall. The flow is not what is broken.

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

**The `reset` flag is noise, not a fault.** Measured across a power cycle: both
drivers read clear on a fresh boot, and both read `reset` again after a run of
moves that ended in the stop path, with "no driver faults" reported throughout.
So the flag says the driver was reset at some point - which F112 evidently does
- and nothing about whether the board is driving. The earlier suspicion that a
flagged driver stops driving is **disproved**: with both flagged, a 200 mm
retract ran its full length with the motion bit set continuously.

`IFS_DIAGNOSTICS` desyncs the link. The F13 immediately after it fails to read
and the next one succeeds, and its own output varies between calls (`stall
counts` on one, `raw silk` on the next), so it is mis-slicing the board's reply.
Read it, then throw the next status away.
