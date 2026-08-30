# AD5X IFS: what zmod does, and what we have

ZMOD is the only working AD5X filament-system implementation, and it is the
reference for this port. This maps its surface onto ours so a gap is visible
rather than discovered on the printer.

Read it with `AD5X_IFS_PROTOCOL.md`, which covers the wire protocol itself.

Sources, all in ZMOD 1.7.1:

- `mod/_mod/.shell/zmod_ifs.py` - the klipper extra
- `mod/_mod/translate/en/ad5x.cfg`, `ad5x_display_off.cfg` - the macros

## The rules the board actually enforces

Each of these is enforced by the board, not by convention, so they lead.

**A command is acknowledged before it is finished.** `F24 ok. chan 1.` says the
board took the opcode, not that it clamped. Send the next opcode into that
window and it answers `FFS not ready.`. zmod follows F24 and F39 with
`wait_for_state()`. Both halves are required: check the ack, then wait.

**Success is a return to READY, never the arrival of an activity state.**
`wait_for_state` returns `RET_OK` on `state == FFS_STATUS_READY`. The
per-channel activity (`CLAMPED`, `LOADING` + 11×(port-1)) is only the window in
which silk and stall are judged. Waiting for an activity to *arrive* times out -
and not because the board skips it, but because it can be over before the next
poll: `clamped` was measured at 3.8 s on one channel and 0.2 s on another in the
same session.

**Only one lane may hold the path into the extruder.** All four converge at a hub
on the toolhead, so a lane threaded there blocks the rest, and the toolhead
sensor cannot see it - a threaded lane parks SHORT of the sensor, which reads
empty while the path is taken. Threading a second lane into an occupied hub jams
both, and the symptom is a feed that stalls at exactly the distance the first
lane had backed off.

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

**One reader.** Every exchange with the board goes through the poll thread, via
`run_operation`. The link is not thread-safe and the poller is always mid-F13,
so a second reader does not get a clean answer - it gets whichever line arrives
first, and so does the poller.

**Counts:** `stall_count` 3, `silk_count` 1, `retry_count` 3.

## Two fields that mean the opposite of their name

**`ffs_channels_insert` is a request queue.** F23 CLEARS the bit, so a set bit
means "please thread this lane", not "this lane is threaded". Measured: after
threading channel 4 and acknowledging it, channel 4 left the mask while channels
1 and 2, whose threading had failed before their F23, stayed in it. Our status
field is `pending_insert_channels` for that reason.

**`FFMInfo.channel` is the current lane, not the lane count.** It reads 4 on a
four-lane machine with lane 4 loaded, which fits both readings at once, and we
had been deriving the channel count from it while zmod reads it as the active
lane. The count is now counted from the `ffmType<n>` keys, which needs no
interpretation. Slot 0 is where a single-material AD5M records what is loaded;
on a machine with an IFS it stays empty, which is why `IFS_MATERIALS` used to
say "loaded: none" whatever was in the extruder.

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
| `IFS_MOTION` | has it stopped / run out | `IFS_MOTION` |
| `INSERT_PRUTOK_IFS` | load to nozzle | `IFS_LOAD` |
| `_IFS_REMOVE_PRUTOK` | out of the nozzle (70 mm) | `IFS_UNLOAD` |
| `_REMOVE_PRUTOK_IFS` | out of the IFS (1000 mm) | `IFS_EJECT` |
| `PURGE_PRUTOK_IFS` | purge only | `IFS_PURGE` |
| `IFS_REMOVE_CURRENT_PRUTOK` | unload whatever is active | folded into `IFS_LOAD` |
| `SET_CURRENT_PRUTOK` / `SET_EXTRUDER_SLOT` | record the active lane | `save_variables` `ifs_loaded` / `ifs_at_hub` |
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
   `IFS_REMOVE_CURRENT_PRUTOK`, which runs `_IFS_REMOVE_PRUTOK` - the SHORT
   path, 70 mm, leaving the lane threaded. Not `_REMOVE_PRUTOK_IFS`, the 1000 mm
   one that pushes the lane out of the IFS; the names differ only in word order
   and using the wrong one ejects the outgoing lane in the middle of a tool
   change. Ours is `IFS_LOAD` calling `IFS_UNLOAD`, falling back to
   `_IFS_CLEAR_EXTRUDER` when no lane is recorded and there is nothing to
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
   cycle with filament still loaded, so a swap would have loaded a second lane
   on top of the first. zmod reads the loaded lane out of `FFMInfo.channel`.
   That file is present and we do read it (`[ifs_materials]`), but nothing
   writes that field once the stock UI is not running, so under Forge-X it is a
   stale number. Ours lives in `save_variables` instead: `ifs_loaded` for the
   nozzle and `ifs_at_hub` for the shared path, written by the load, cleared by
   the unload, and read by `IFS_UNLOAD`, `IFS_EJECT`, `IFS_MOTION`, `IFS_PURGE`
   and the auto-insert guard.

**Nothing on zmod's command surface is missing now.** What is left is the
behaviour it never had: `IFS_EJECT` for taking a lane out of the machine, hub
ownership so two lanes cannot be threaded into the same place, and a gate
(`tools/lint/check_gcode_safety.py`) against the exception that shuts klippy
down.

### Eject - `_REMOVE_PRUTOK_IFS` ("Unloading filament Extruder + IFS")

This is the 1000 mm one, our `IFS_EJECT`. The short 70 mm sibling that a tool
change wants is `_IFS_REMOVE_PRUTOK`, our `IFS_UNLOAD`.

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

`_SBROS_TRASH_DAVIM PRUTOK=0` then `_SBROS_TRASH` then `_CLEAR_REZINA`, and
`IFS_PURGE` is ours. Note that zmod shakes and wipes after **every** purge pass,
not only the last: leaving the first blob attached carries it into the second
pass and then onto the wiper, which smears it rather than removing it.

### Positions

| zmod | Ours |
|---|---|
| `_GOTO_TRASH` / `_GOTO_TRASH_STANDARD` | `_IFS_PARK_FOR_PURGE` via `_IFS_GOTO_STATION` |
| `_CLEAR_REZINA` | `_IFS_WIPE` |
| `_SBROS_TRASH` (shake off, no extrusion) | `_IFS_SHAKE` |
| `_MOVE_TO_CUT_PREPARE_POSITION` | folded into `_IFS_CUT`, which travels to the clear position, drags the shear, and withdraws the stub as one sequence |

## Where copying zmod turned out to be right

- **The toolhead sensor's thresholds are stock's**, 0.30 and 0.72. Sweeping the
  sensor - the tip retracted a millimetre at a time and pushed back - shows one
  proximity curve, not clusters: flat near 0.008 while a strand covers the
  sensor, through a knee at 9 mm, resting at 0.023 after a completed load, and
  only reaching 0.398 with the toolhead genuinely empty. 0.30 sits in the only
  real gap, six times above the highest present reading and a quarter below the
  lowest absent one; anything inside the curve misreads one end or the other.

- **The extruder withdraw co-pulls with the lane.** zmod's `_IFS_REMOVE_PRUTOK`
  issues `G1 E-nozzle_cleaning_length` and an `IFS_F11` of the same length at
  the same speed back to back, so the queued G1 runs alongside the lane's own
  retract. Ours did the extruder move alone, with the lane released, which
  drags 60 mm of filament backwards against an idle drive. It is the same rule
  the purge already follows in the other direction: **the IFS and the extruder
  move one strand, so they move together or they fight.**

- **A tool change is a gcode_macro, not a patched `virtual_sdcard`.** zmod
  intercepts the `T<n>` line inside `work_handler`, fires
  `_A_CHANGE_FILAMENT`, then spin-waits at 2 Hz on a boolean that
  `END_CHANGE_FILAMENT` clears - and the print hangs forever if it never does.
  Klipper already runs a `[gcode_macro T2]` synchronously in the gcode stream,
  which is that behaviour without the patch, the polling, or the deadlock. It
  also means no fork of a klipper internal to carry forward.
- **The lift is the reason the restore is short.** zmod lifts 5 mm and then
  walks the head around the bed edges, choosing rails by midpoint distance,
  because at 5 mm it is still in among the part. `_IFS_GOTO_STATION` already
  lifts 50 mm on the way out, so the way back is: leave the back edge, X, Y, Z
  last. The one rule that survives is the one that was ever about the machine
  rather than the part - **X never travels while the head is behind `safe_y`**,
  because that is where the wall hardware is.

- **A stalled load feed is not fatal.** The load feed ENDS by arriving at the
  extruder gear, and the IFS cannot push past a gear that is not turning - so
  stopping short is how the feed finishes and the co-push completes it. Failing
  the load there aborts it before the one step that can save it. Scope the
  softness: a stall can also be a real obstruction at the combiner, which needs
  a hand, and the soft path has never fired on this printer. A healthy lane
  reaches the toolhead sensor, which sits UPSTREAM of the extruder gear.

- **Both purge passes go to the chute.** `_SBROS_TRASH_DAVIM` opens with
  `_GOTO_TRASH` and zmod calls it once per pass, so both of its purges land in
  the bin. A pass that purges wherever the last wipe left the head fires onto
  the wiper pad - and purging onto the wiper is how the wiper stops being one.

## Deliberate deviations

Everything else should copy zmod. These do not, for stated reasons:

- **Payload checking lives in one place.** zmod repeats the expected reply at
  each call site; ours is `IfsOperations._expect`, so a command cannot forget.
- **The pre-command status is skipped by identity** (`_fresh_status`) rather
  than avoided by polling quickly. Stronger than a timing assumption.
- **A move says HOW it ended, but a missed sensor is not a failure.** zmod's
  checked feed returns `RET_OK` when the board simply finishes; ours reports
  `not_reached` and carries on. Acting on the difference stops loads whose
  filament is already at the toolhead entry, before the co-push that would pull
  it in. Reporting the difference is useful; acting on it is not.
- **A move can back off through the feed** (`BACKOFF=`), applied only when the
  sensor is what ended it. zmod decides the same thing in python off
  `RET_EXTRUDER`. It cannot live in the macro: a klipper gcode_macro is rendered
  ONCE, before any of it runs, so a sensor read written after a feed already
  happened before the feed was sent.
- **Only one lane may hold the shared path**, tracked in `save_variables`
  (`ifs_at_hub`). zmod threads every inserted lane to the toolhead and packs
  the hub; the toolhead sensor cannot catch this, because a lane threaded but
  not loaded sits SHORT of the sensor.
- **UNLOAD and EJECT are different commands.** zmod's `_IFS_REMOVE_PRUTOK`
  (70 mm, lane stays threaded) and `_REMOVE_PRUTOK_IFS` (1000 mm, lane leaves
  the IFS) differ only by the order of three words in their names, and using the
  second where the first belongs ejects the outgoing lane during a tool change.
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
- **The cut is muted, blade moves included.** zmod brackets only the two `G1 E`
  moves of `_CUT_PRUTOK` with `_DISABLE_SENSOR`/`_ENABLE_SENSOR` and runs the
  shear itself with the sensor live. Severing the filament is the largest
  present-to-absent step there is; on a sensor declared `pause_on_runout` it
  fires a runout, and a soak run paused itself mid-cut on an idle machine.
- **A sensor fault is not filament.** zmod's table reports PRESENT above 0.72,
  which is the reading a disconnected sensor rails to. Same runout decision as
  ours while `fail_safe` is on, but ours calls it FAULT so `IFS_SENSOR_VALUE`
  and `classify()` say what is actually wrong.
- **A leaked sensor mute is healed, not inherited.** Klipper macros have no
  `finally`, so an error between `_IFS_SENSOR_HOLD` and its resume leaves the
  toolhead sensor muted - and the printer then runs with no runout detection at
  all, silently, because a muted sensor is indistinguishable from a sensor with
  filament in front of it. Every public entry point restores it before it moves
  anything. zmod has the same hole and no heal.
- **The shared path is claimed before it is entered.** zmod records the loaded
  lane only once the load has worked. A feed that stalls part-way still leaves
  the lane in the tube, so the record reads "nobody" while somebody is there,
  and the next load of a different lane feeds into it. Claiming first makes an
  accurate record the recovery, with no new code path.
- **Per-material temperatures refuse rather than guess.** zmod silently
  substitutes PLA for a material it has no entry for, which runs an unknown
  filament at 220 and snaps it off in the heatbreak. Ours reports no temperature
  and the load insists on an explicit `TEMP=`.

- **The load asks the toolhead sensor AFTER the purge** (`IFS_REQUIRE_TOOLHEAD`).
  zmod never asks at all: a lane that never arrived is purged as air and
  recorded as loaded. A soft feed cannot answer the question on its own -
  arriving at the extruder gear and never arriving end the feed identically -
  so the check belongs after the extruder has had its turn at the filament.

- **A swap retracts 130 mm, not 430.** The outgoing lane is moved by
  `IFS_UNLOAD` alone: 60 mm through the extruder and 70 mm more, stock's own
  `UnloadESpace` + `UnloadIFSSpace`. The extruder tip is about 150 mm above the
  combiner, so that clears the shared path with margin, and the lane is left
  staged for its own next load. The extra `hub_clear_mm` retreat now fires only
  for a lane PARKED at the hub without being loaded, which is what a failed load
  leaves behind.

- **An eject gives up the lane's claim on the shared path.** The filament is no
  longer in the machine to hold it. Measured: ejecting a parked lane 1 left
  `ifs_at_hub=1` with the spool on the bench, and the next insertion of lane 4
  was refused by a lane that was not in the printer.

## What the "it will not feed" night actually was

Every lane appeared to stall on a feed while retracting perfectly, hot or cold,
and each retry moved less than the last (110 mm, 230, 40, 25, 30). That reads
exactly like a ground-flat filament or a spool that will not pay out, and it was
called both. It was neither. **Preston pulled a lane and its tip was perfectly
blunt.**

Two causes, both ours:

**The hub can only hold one lane.** Auto-insert threaded lane 4 to the toolhead
sensor and parked it 90 mm back, then threaded lanes 2 and 1 straight into it.
Lane 4's next load stalled after exactly the 90 mm it had backed off - against
them. Retracting the other two 200 mm each and it went straight through with
40 mm to spare. The toolhead sensor cannot see this, because a lane threaded but
not loaded sits SHORT of the sensor: the sensor says empty while the path is
taken. That is why the lane holding it is recorded rather than inferred.

**A feed that finished without the sensor was being failed.** zmod returns
`RET_OK` there and its macro carries straight on to the co-push, where the
EXTRUDER gear pulls the filament the last stretch in. Ours aborted instead, so a
load whose filament was already at the toolhead entry - "stuck at the toolhead
entry, the filament isn't gripped by the gears yet" - never reached the step
that would have grabbed it. Running the co-push by hand finished it immediately.

**The diagnostic lesson.** Feed-stalls-while-retract-works is not by itself a
verdict on the drive. On a system where several lanes converge on one point,
check what else is parked in that point first. Two independent lanes failing the
same way was read as "therefore the hardware", when it was really "therefore
something they share".

The board's per-lane stall counter (`IFS_DIAGNOSTICS`) is a genuine signal:
lane 3, which has never held filament, reads 0, and a lane whose commanded feed
did not move gains hundreds.

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
