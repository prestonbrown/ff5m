# AD5X IFS wire protocol

The AD5X's 4-channel filament system (IFS) is a separate STM32 board on the host
UART `/dev/ttyS4`. This document is derived from **the board's own firmware**, not
from a driver, so it is ground truth - including opcodes no driver uses.

Reproduce it:

```bash
python3 tools/ifs/extract_ifs_protocol.py /usr/prog/PROGRAM/control/ifs.hex
```

`ifs.hex` is Intel HEX, base `0x08010000`, 41120 bytes; vectors check out as
Cortex-M (SP `0x20007498` in SRAM, reset `0x080101AD`, odd = Thumb).

Related: `docs/FIRMWARE_5x_COMPAT.md` for the stock-firmware compatibility notes
this sits alongside.

> **`strings(1)` does not find these.** Use an explicit printable-run regex. And
> do not filter runs by "looks like a word" - that drops `F15 ok.` and `F18 ok`
> and undercounts the opcode table by a third.

## Wire

Everything in this section is **measured** - see "Ground truth" below.

| | |
|---|---|
| Port | `/dev/ttyS4`, held by `firmwareExe` under stock |
| Baud | 115200, 8N1 (read off the live termios) |
| Request | `<command> \r\n` - note the **space before CRLF** |
| Reply | bytes, **no terminator**; the reply ends when the board goes quiet |
| First byte | ~105 ms after the command |
| Whole reply | ~165 ms for a 127-byte `F13`, reading in blocks |
| Poll cadence | stock sends `F13` every ~0.91 s, and nothing else while idle |
| Readers | **exactly one**, always |

**One reader, always.** A reply has no terminator and ends when the board goes
quiet, so two readers on the port do not each get a clean answer - they get
whichever bytes arrive first, and so does the other. The symptom is quiet
corruption: diagnostics output that varies between calls and status polls that
come back empty, reading exactly like a disconnected board. Everything must go
through the one thread that polls.

The board is not slow. Watching `firmwareExe` suggests ~4.6 ms per byte, but that
is its own read loop - it calls `read()` for **one byte at a time**, so a 127-byte
reply is 127 syscalls. Reading in blocks gets the same reply in ~165 ms.

**The `0xFF` commit byte is a host habit, not a protocol requirement.** The stock
FlashForge host sends one ~200 ms after every command, but the board does not wait
for it: all four combinations return an identical 127-byte `F13` reply when sent
directly to the board:

| command sent | reply |
|---|---|
| `F13 \r\n` - space only (stock form, as captured) | yes |
| `F13\r\n` + `0xFF` | yes |
| `F13\r\n` - neither | yes |
| `F13 \r\n` + `0xFF` - both | yes |

So the trailing space and the commit byte are both **cosmetic** on firmware 3.0.6.
Sending the commit byte also forces a delay before it, which is a hard 200 ms on
every command for nothing.

**There is no `\r\n` at the end of a reply.** A 126-byte `F13` response contains no
CR and no LF anywhere. What delimits a reply is silence, which makes the read timeout
not a safety net but the framing itself. A reader that blocks for a newline waits
forever. (`F21` is the exception: it embeds CRLF *between* its three
lines. See below.)

The trailing space is on every command stock sends - `F13 \r\n`, `F24 C2 \r\n`,
`F10 C2 L600 S1200 \r\n` - and the A/B above shows the board answers without it
too. Both are cosmetic on 3.0.6.

`/proc/tty/driver` shows `uart4` at `tx:0 rx:0` under a Forge-X bring-up, because
we stop the stock UI and ship no IFS driver. Under stock, `firmwareExe` owns it.

## Response framing

Three rules, all read straight off the firmware's format strings, and all three
missing from any driver:

**1. Every response echoes the request opcode: `F<n> ok.`** No string in the
3.0.6 image breaks this. (One external tester recalls a reply variant without
the trailing period on some revision - unconfirmed, but the module's prefix
regex treats the dot as optional, so correlation survives it either way.)
That is free request/response correlation on the wire - a
reply can be matched to its request without driver-side bookkeeping, and a stale
or unsolicited line can be discarded instead of parsed as the answer to whatever
was asked last.

**2. `ok.` is an echo, not a status.** `F10 ok. FFS not ready.` is a *failure*.
Success has to be read from the payload after the prefix, never from `ok.`

**3. The separator after `ok.` is inconsistent.** `F10`-`F24`, `F39` emit
`"F13 ok. FFS_state:"` with a space; `F40`-`F64` emit `"F40 ok.stall count:"`
with none. Split on the `F<n> ok.` prefix and strip - do not split on `"ok. "`.

Most responses also carry a **trailing space** at the end. Strip. Note this is
the reply's last byte, not a separator - there is no CRLF after it (`F21`
excepted, which embeds CRLF *between* its three lines).

### `F21` returns three lines

```
F21 ok. \r\n silk: %d %d %d %d \r\n stall: %d %d %d %d
```

Confirmed on hardware - a live reply, embedded CRLFs and all:

```
F21 ok.
 silk: 199 333 1688 271
 stall: 2048 2128 3417 2146
```

A transport that does one `readline()` per command reads `F21 ok.` and leaves two
lines in the buffer, which then answer the *next* two requests. Every later poll
is off by one, silently. Read until the response is complete, not until the first
newline. No other opcode does this - and note this is the one place a reply
contains CRLF at all, which is why "read one line" is the wrong primitive here
generally.

## Commands

`C` selects a channel (1-4), `L` a length in mm, `S` a speed in mm/min. Three
further argument letters exist that no driver sends: `D` is an absolute selector
position in steps (`F30`), and `T`/`I` are a motor number and a current scale
(`F42`). Every argument is parsed with `strchr` then `strtod`, so order does not
matter and an absent letter is simply skipped.

| Command | Arguments | Meaning |
|---------|-----------|---------|
| `F10 C<n> L<mm> S<speed>` | channel, length, speed | feed filament |
| `F11 C<n> L<mm> S<speed>` | channel, length, speed | retract filament |
| `F13` | none | status poll |
| `F15 C` | literal `C` | reset driver |
| `F18` | none | unclamp all channels - by re-homing the selector |
| `F20` | none | nothing at all; a pure ping |
| `F23 C<n>` | channel | mark filament inserted |
| `F24 C<n>` | channel | clamp channel |
| `F30 D<steps>` | selector position | jog the selector, absolute, no homing |
| `F39 C<n>` | channel | release/unclamp channel |
| `F42 T<1\|2> I<0-31>` | motor, current scale | set that motor's run current |
| `F43` | none | re-initialise both TMC drivers |
| `F112` | none | halt movement |

The last five are decoded in "The five opcodes nobody had pinned down" below.

## The status line

```
F13 ok. FFS_state: %d silk_state: %d chan: %d ffs_channels_insert: %d
        stall_state: %d jinsi_GCONF: %02x%02x%02x%02x
        qiehuan_GCONF: %02x%02x%02x%02x
```

`jinsi` / `qiehuan` are pinyin - 进丝 "feed filament", 切换 "switch". The board
carries **two TMC drivers**: a feeder and a channel selector, and `F13` returns
both their `GCONF` registers.

- `silk_state`, `stall_state` - per-channel **bitmasks**, `(v >> i) & 1`
- `ffs_channels_insert` - per-channel bitmask, and a **request queue**: a set
  bit means "please thread this lane", not "this lane is threaded". See below.
- `chan` - **not the loaded channel.** Observed naming a lane that was not the
  one in the shared path after a completed load, with nothing in the documented
  opcode set bringing it back in step - whatever it tracks is board-internal,
  probably selector-side. `0` means none. Which lane is loaded is host-side
  state (`save_variables` `ifs_loaded` under the macros in this repo); never
  read `chan` as that answer.

### `stall_state` reports MOTION, not a stall

The bit is **set while that channel's filament is moving** and clear when it is
not. Measured with an empty channel as the control:

| | `F10` reply | motion bit during the feed |
|---|---|---|
| channel 3, **empty** | `F10 ok. FFS not ready.` (refused) | clear |
| channel 1, **loaded** | `F10 ok. FFS channel 1 feeding.` | **set**, then clear when done |

The same bit sets for the duration of an `F11` retract. A stall, then, is the
*absence* of this bit sustained over several polls while a move is commanded -
a single clear sample is noise, because the bit also toggles as the motor steps.

So a jam is the *absence* of this bit while something was told to move. Reading
it as "stalled" inverts every motion check - healthy moves look jammed and jams
look healthy.

Note also that on **3.0.6** the board **refuses to feed an empty channel**: `F10`
on a lane with no filament answers `FFS not ready.` That refusal is a firmware
behavior, not a protocol invariant - on **3.0.7 it is gone** (externally
confirmed: `F10` and `F11` run with no channel clamped at all). A driver must
gate feeds on its own presence data; this module's `loaded_channels` check is
that gate, and the refusal was only ever a redundant backstop for it.

### Where `channel_count` comes from: nowhere, on this firmware

A `channel_count` field that some drivers parse out of `F13` **is not in 3.0.6's
format string** - the regex never matches and the value stays 0. (The field
originates in the IFS Jacker companion below, which multiplexes several IFSes
onto one link; it was never the board's own.) The field list
does move across revisions: a captured 3.0.5 `F13` reply carries a trailing
`vibr: %d` that 3.0.6 never emits. On 3.0.6 the fields present are exactly
`FFS_state`, `silk_state`, `chan`, `ffs_channels_insert`, `stall_state`,
`jinsi_GCONF`, `qiehuan_GCONF`.

Nothing on the wire names the channel count except `F19` - ask it, rather than
assuming four. See below.

### State values

`FFS_state` is not a status word - it is the program counter of a state machine
the board runs at a fixed tick, and every opcode that actuates anything works by
writing a number into it. The dispatch is a jump table of 132 entries at
`0x08018FF4`, indexed by the state byte at `0x200001B0`; anything at or above
132 falls to a do-nothing default. Decoded from it:

| State | Meaning |
|------:|---------|
| 1 | begin homing the selector - read the home switch, pick a direction |
| 2 | homing, phase 1: walk 4096 steps to get clear of the switch |
| 3 | homing, phase 2: seek the switch, giving up after 16384 steps |
| 4 | homed; dispatch to whichever channel is pending |
| 5 | **ready** - the only state in which `F10`/`F11` are accepted |
| 126 | homing failed - the switch never appeared |
| 127 | driver error |
| 128 | `F30` jog: energise the selector |
| 129 | `F30` jog: step toward the target, then stop and **stay here** |

and then a per-channel block, base plus a stride of 11 (`FFS_STATUS_DELTA`) for
channels 1-4:

| Base | ch 1-4 | Meaning |
|-----:|--------|---------|
| 7 | 7, 18, 29, 40 | driving the selector to that channel's clamp position |
| 10 | 10, 21, 32, 43 | feed started (`F10`) |
| 11 | 11, 22, 33, 44 | **loading** - waiting on the feeder |
| 12 | 12, 23, 34, 45 | driving the selector to that channel's release position |
| 14 | 14, 25, 36, 47 | retract started (`F11`) |
| 15 | 15, 26, 37, 48 | **unloading** - waiting on the feeder |
| 16 | 16, 27, 38, 49 | post-retract cleanup, then back to `ready` |

Two corrections to what was inferred from observation alone. **3 is not
"polling channels"** - it is the second half of a selector homing walk, which is
why it appears where a poll would. And **2 is not a brief unnamed busy state** -
it is the first half of that same walk, and it is brief only because it is a
fixed 4096 steps.

`clamped` (7) is real but its duration is not predictable - measured at 3.8 s on
one channel and 0.2 s on another in one sequence of clamps. Anything that waits
for it to *arrive* will sometimes miss it entirely. Wait for the return to
`ready` instead. Those two numbers are not noise: the state is an absolute move
to a position 0, 4096, 8192 or 12288 steps from home, and 12288 steps at the
rate derived in "The selector motor" below is 3.83 s. The 3.8 s reading was
channel 4 and the 0.2 s reading was channel 1, which is already there.

### Sequencing rules the board enforces

Measured, each one the hard way:

- **A command is acknowledged before it is finished.** `F24 ok. chan 1.` means
  the board took the opcode, not that the clamp closed. Send the next opcode
  into that window and it answers `FFS not ready.`. Check the ack, then wait
  for the state to come back to `ready`.
- **Success is the return to `ready`, never the arrival of an activity state.**
  The per-channel activity is only the window in which sensor conditions are
  judged, and it can be over before the next poll: `clamped` measured at 3.8 s
  on one channel and 0.2 s on another in the same session. Waiting for an
  activity to *arrive* times out on a board that is working perfectly.
- **Refusals are prefixed like successes.** `F10 ok. FFS not ready.` is a
  refusal and `F10 ok. FFS channel 1 feeding.` is a success; only the payload
  separates them. Every opcode must be checked against its expected reply.
- **Judge motion at a fast cadence.** The motion bit toggles; sampling it once
  a second reads a running motor as stalled. While a move is being judged,
  poll at 0.2 s and require several consecutive clear samples.

## Every opcode the firmware answers

32 opcodes, 58 distinct response strings. Responses below are verbatim,
including the `F<n> ok.` prefix (`%d`/`%02x` are the firmware's own format
specifiers). Nine of them are enough to drive the whole machine (the operations
layer); the rest are diagnostics and capability reads.

| Opcode | Response |
|--------|----------|:---:|
| `F10` | `F10 ok. FFS channel N feeding.` / `... FFS channel not exist.` / `... FFS not ready.` / `... No channel selected.` |
| `F11` | `F11 ok. FFS channel N exiting.` + same error set |
| `F12` | `F12 ok. %d %d %d %d` |
| `F13` | the status line above |
| `F14` | `F14 ok. stall: %d %d %d %d` |
| `F15` | `F15 ok.` |
| `F18` | `F18 ok` *(no trailing period)* |
| `F19` | `F19 ok. four color. version: 3.0.6` | **no** |
| `F20` | `F20 ok.` |
| `F21` | `F21 ok.` + ` silk: %d %d %d %d` + ` stall: %d %d %d %d` **(3 lines)** |
| `F22` | `F22 ok. ffs_channels_insert: %d` |
| `F23` | `F23 ok. chan N.` / `F23 ok. no chan.` |
| `F24` | `F24 ok. chan N.` / `... FFS channel not exist.` / `... No channel selected.` |
| `F30` | `F30 ok.` |
| `F39` | `F39 ok. FFS channel N release.` + error set |
| `F40` | `F40 ok.stall count: C1: %d C2: %d C3: %d C4: %d` |
| `F41` | `F41 ok.GCONF: %02x%02x%02x%02x` |
| `F42` | `F42 ok.stepper_motor: %d stepper_motor_irun: %d` |
| `F43` | `F43 ok.` |
| `F44` | `F44 ok.DRV_STATUS: %02x%02x%02x%02x` |
| `F45` | `F45 ok.GSTAT: %02x%02x%02x%02x` |
| `F50`-`F54` | `GCONF`,`GSTAT`,`CHOPCONF`,`DRV_STATUS`,`PWMCONF` - driver 1 |
| `F60`-`F64` | the same five - driver 2 |
| `F112` | `F112 ok.` |

## What the unused two-thirds buys us

All values below were read off a live board (firmware 3.0.6, four channels, with
filament in 1, 2 and 4).

- **`F19` is a capability probe, and it answers.** `F19 ok. four color. version:
  3.0.6`. It is a **literal** with no format specifiers, so both the count word
  and the version are baked into each firmware build and a different board
  answers with its own. Every driver today assumes four channels; this asks.

- **`F21` returns raw per-channel sensor values, not bitmasks:**

  ```
  F21 ok.
   silk: 199 333 1688 271
   stall: 2048 2128 3417 2146
  ```

  Compare `F14`, which returns `stall: 0 0 0 0` for the same instant, and `F13`,
  whose `silk_state`/`stall_state` are single bits per channel. So `F13` and `F14`
  give the thresholded answer and **`F21` gives the underlying measurement**. A
  channel that is marginal - filament present but barely triggering - is visible
  in `F21` and invisible everywhere else. No driver reads it.

- **`F40` returns per-channel counters labelled `stall count`**, e.g.
  `stall count: C1: 1 C2: 462 C3: 1 C4: 1`. **Not monotonic**: the same counter
  read 462, then 30 after a clamp/retract/feed/release cycle that moved other
  lanes. Something resets or windows it. Do not treat it as a lifetime total.

- **Both TMC drivers are readable.** `F41`/`F44`/`F45` and `F50`-`F54` (driver 1)
  and `F60`-`F64` (driver 2) expose `GCONF`, `GSTAT`, `CHOPCONF`, `DRV_STATUS`,
  `PWMCONF`. Observed at idle:

  | register | driver 1 | driver 2 |
  |---|---|---|
  | `GCONF` | `000001dc` | `000001dc` |
  | `GSTAT` | `00000001` | `00000000` |
  | `DRV_STATUS` | `80000000` | `00000000` |
  | `CHOPCONF` / `PWMCONF` | `00000000` | `00000000` |

  `GSTAT` bit 0 is the TMC reset flag and `DRV_STATUS` bit 31 is `stst`
  (standstill), both plausible at idle. `F41` returned the same `GCONF` as `F50`,
  so it likely aliases the F50 bank. Overtemperature, open-load and short
  detection all live in `DRV_STATUS` and reach no UI today.

  **`CHOPCONF` and `PWMCONF` reading `00000000` was a broken read, not a real
  value - since confirmed.** The firmware writes both at boot, to `0x150082C3`
  and `0xC80D174B`, and a re-probe of the same board on 2026-09-01 read back
  exactly those values on **both** banks:

  | register | firmware writes | `F52`/`F62` read back |
  |---|---|---|
  | `CHOPCONF` | `150082c3` | `150082c3` |
  | `PWMCONF` | `c80d174b` | `c80d174b` |

  So treat an all-zero answer from `F52`/`F54`/`F62`/`F64` as "no answer", not
  as data - the earlier capture caught the read path failing, most likely a
  diagnostics batch interleaving with the status poll. The `CHOPCONF` value
  settles the microstepping: `MRES` (bits 27-24) is 5, which is **1/8
  microstepping**, with `intpol` set so the driver interpolates to 256 inside.

  **Which bank is which, settled by experiment.** Both read identically at idle,
  so they were separated by making each motor move and watching the standstill
  bit drop:

  | moved by | bank that lost `stst` | so it is |
  |---|---|---|
  | `F24 C2` (select a channel) | `F60`-`F64` | the **selector** (`qiehuan`, 切换) |
  | `F11 C2 ...` (move filament) | `F50`-`F54` | the **feeder** (`jinsi`, 进丝) |

  That matches the two `GCONF` values `F13` already reports under those names.
  While moving, `DRV_STATUS` read `00090000` - bits 16-20 are `CS_ACTUAL`, the
  current scale actually applied, which independently corroborates the register
  layout.

  **The part is almost certainly a TMC2209.** Both drivers report
  `GCONF: 000001dc`, which sets `pdn_disable` and `mstep_reg_select` together -
  the canonical configuration for driving a 2209 over UART, which is how this
  board does it. Read as a TMC2130 the same value would mean `enc_commutation`
  on a filament feeder, which makes no sense. Strong but circumstantial, so the
  fault-bit decoding names the family it assumed and an unknown family still
  reports that *some* fault bit is set rather than staying silent.

- **`F20`, `F30` and `F43` are decoded now** - see the next section. Two of the
  three are safe; `F30` actuates.
- `F12` answers with four numbers: per-channel filament presence, the decimal
  form of `F13`'s `silk_state`.
- `F37` is reported by an external capture (a Telegram-circulated IFS table) to
  put the board into **firmware-update mode**. Never probe it from a working
  install - though it is recoverable: the native screen's IFS firmware update
  reflashes the board (close the port, run
  `/usr/prog/PROGRAM/control/IFSCommand /usr/prog/PROGRAM/control/ifs.hex
  /dev/ttyS4`, re-probe with `F19`), and the board comes back.

## The five opcodes nobody had pinned down

`F18`, `F20`, `F30`, `F42` and `F43` all answer with a bare `ok` that says
nothing about what they did, so they sat in "acknowledges, effect unknown" for a
long time. GhostTypes' firmware analysis, posted to Discord, named all five;
the decodes below were then read out of the same `ifs.hex` this document is
built from, which both confirms them and adds the parts a response-string sweep
cannot see. Handler addresses are given so any of it can be checked:

```bash
arm-none-eabi-objdump -D -b binary -m arm -M force-thumb \
    --adjust-vma=0x08010000 ifs.bin | less    # ifs.bin from --bin above
```

**`F18` (`0x080134FE`) writes `FFS_state = 1` and nothing else.** State 1 is the
head of the selector homing walk, so the observable effect - every channel ends
up unclamped - is a *consequence* of driving the selector off all four slots, not
a clamp-release operation. It takes as long as a home takes.

**`F20` (`0x08013528`) is three instructions: format the reply, send it, return.**
It touches no state, no GPIO and no driver. It is a genuine no-op and therefore
the correct liveness ping - unlike `F13`, whose reply has to be parsed, and
unlike `F19`, which is a capability read some other firmware may answer slowly.

**`F30 D<n>` (`0x08013738`) is a raw selector jog, and it is the only opcode that
moves the selector without homing first.** It stores `n` at `0x200000DC` and
writes `FFS_state = 128`. States 128 and 129 then energise the selector and step
it until its position counter equals `n`. That counter, at `0x200001E0`, is the
same one every other selector move uses, so **`D` is in the slot ladder's own
coordinates** - `F30 D4096` lands exactly where `F24 C2` would.

Three things make it sharp:

- With no `D`, the `strchr` misses, the state write is skipped, and it is as
  inert as `F20`. Bare `F30` is safe. `F30 D<n>` actuates immediately.
- It bypasses the whole clamp/release sequence. Nothing checks that a lane is
  free, nothing updates `chan`, nothing arbitrates against a feed in flight.
- **It never returns to `ready`.** State 129 stops the motor when it arrives and
  stays at 129 for ever, so `F10`/`F11` answer `FFS not ready.` from then on.
  `F15 C` is the escape hatch - its handler forces `FFS_state = 5`.

**`F42 T<motor> I<value>` (`0x08013B3A`) sets a run current, and the reply is not
a read.** `T` (1 = feeder, 2 = selector) and `I` are each parsed independently
and stored in a pair of shadow bytes at `0x200000F1`/`0x200000F2`; only if `I`
was present does the board write TMC register `0x10` (`IHOLD_IRUN`) as
`0x000A0000 | (I << 8)` - that is `IRUN = I`, `IHOLDDELAY = 10`, and
**`IHOLD = 0`**, no holding current at standstill. Which UART each `T` goes to
matches the bank split established above: `T1` is the feeder's, `T2` the
selector's.

| behaviour | consequence |
|---|---|
| the reply echoes the **shadow bytes**, never the driver | a bare `F42` on a fresh board answers `irun: 0` while the driver is really at 9 |
| `T` is sticky across commands | `F42 I20` with no `T` writes to whichever motor was named last |
| `I` is stored through a **byte** store, then clamped | `I-1` becomes 255, which clamps to **31 - full current**, not to 0 |
| an unrecognised `T` writes no register | but still replies `ok`, so a typo is silent |

The stock host never sends `F42`, so the default has to come from the boot
initialisation, and it does: **`IRUN = 9`** of 31. That is independently
confirmed on hardware - `DRV_STATUS` read `00090000` while moving, and bits
16-20 are `CS_ACTUAL`, the scale the driver is actually applying.

**`F43` (`0x08013C26`) re-runs the full TMC initialisation on both drivers.**
Not a UART reset - it rewrites five registers, the same values the board writes
at boot:

| register | value | meaning |
|---|---|---|
| `GCONF` `0x00` | `0x000001DC` | spreadCycle, `shaft` inverted, `pdn_disable`, `mstep_reg_select` |
| `IHOLD_IRUN` `0x10` | `0x000A0900` | `IHOLD` 0, **`IRUN` 9**, `IHOLDDELAY` 10 |
| `TPOWERDOWN` `0x11` | `0x80` | ~2.8 s standstill before current drops |
| `CHOPCONF` `0x6C` | `0x150082C3` | `TOFF` 3, `TBL` 1, **`MRES` 5 = 1/8**, `intpol` on |
| `PWMCONF` `0x70` | `0xC80D174B` | stealthChop PWM parameters |

Two things follow. It is the **selector driver reset this repo has been missing**
- `F15 C` only drops the feeder's enable line and forces `FFS_state = 5`; it
writes no TMC register and touches the selector not at all. And it **discards
any `F42` current** you set, since it rewrites `IHOLD_IRUN` to the stock 9. It
also does not touch `FFS_state`, so sending it mid-move leaves the state machine
stepping a driver that is being reconfigured underneath it.

## The selector motor: how fast it goes, and why

The recurring complaint about the IFS is that the channel selector is slow. It
is, and the reason is not the step rate.

**The mechanism is an eight-stop turret.** Every selector position in the
firmware is a multiple of 2048 steps, and the homing seek gives up after 16384 -
one full revolution, eight stops:

| position | 0 | 2048 | 4096 | 6144 | 8192 | 10240 | 12288 | 14336 |
|---|---|---|---|---|---|---|---|---|
| stop | ch1 clamp | ch1 release | ch2 clamp | ch2 release | ch3 clamp | ch3 release | ch4 clamp | ch4 release |

**The steps are bit-banged out of a timer interrupt.** There is no hardware PWM,
no DMA pulse train and no ramp. TIM6's ISR (`0x08018FDC`) runs the state machine,
and every state that moves the selector does exactly one thing per tick: toggle
`PA7`. Two ticks make one step; `PA6` is direction, `PB1` enable, `PB9` the home
switch. The rate is therefore the timer's update rate divided by two, and it is
the same for homing, for slot moves and for `F30`.

**That rate is a compile-time constant.** From the timer setup at `0x08015798`:

- `ARR = 155`, so one update every 156 counts.
- `PSC = SystemCoreClock / 2000000 - 1`.
- `RCC_CFGR.PPRE1` is set to `/4` at `0x08012070`, so TIM6's input is
  `2 x PCLK1 = HCLK/2`.

Those cancel: if `SystemCoreClock` is HCLK - which it has to be for the SysTick
setup at `0x08015826` (`SystemCoreClock / 50`) to mean anything - the counter
runs at **1 MHz whatever the core clock is**, the ISR fires at
`1000000 / 156 = 6410 Hz`, and the selector steps at **3205 steps/s**. For the
record the core clock is 54 MHz: `HSE_VALUE` is 12 MHz (`0x00B71B00`, in
`RCC_GetClocksFreq`) and the PLL runs `HSE/2 x 9`.

**Measured on hardware: 3190 steps/s**, 0.5% off the derivation. See "What the
measurement said" below.

At 1/8 microstepping that is ~400 full steps/s, and 16384 steps per turret
revolution against a 200-step motor puts about 10:1 of reduction in between. The
motor is turning at roughly 120 rpm. Nothing about that is near a limit.

**The cost is distance, not speed.** `F24 C<n>` does not move to the adjacent
slot. Every channel select writes `FFS_state = 1` - all four of them, `C1`
included - which is a full re-home before the move:

| phase | steps | at 3205 steps/s |
|---|---|---|
| walk clear of the home switch (only if it is already made) | 4096 | 1.28 s |
| seek the switch | 0 - 16384 | up to 5.11 s |
| absolute move to the slot | 0, 4096, 8192 or 12288 | up to 3.83 s |
| **worst case** | **32768** | **~10.2 s** |

A slot-to-slot move is 4096 steps, 1.28 s. The board spends the rest of a tool
change re-discovering where it already was.

### Room to change it

| lever | effect | needs |
|---|---|---|
| `F42 T2 I<n>` | selector **torque**, not speed | nothing - it is on the wire today |
| skip the re-home with `F30` | up to 10.2 s becomes 1.28 s | host-side position tracking, and accepting that `F30` bypasses every interlock |
| TIM6 `ARR` | the step rate itself | reflashing `ifs.hex` |

There is no speed argument for the selector anywhere in the protocol. `F24` and
`F30` take no `S`, the state machine holds no rate variable, and the TMC drivers
are stepped over STEP/DIR - the UART side sets current and microstepping, never
velocity. `F42` is the only writable driver register the firmware exposes, and it
writes `IHOLD_IRUN` alone; there is no path to `CHOPCONF`, so microstepping
cannot be dropped from the host either.

So the honest answer to "can we speed it up" is: **not from the host, and the
step rate is not where the time goes anyway.** The interesting lever is `F30`,
because avoiding the re-home is worth several seconds per tool change and costs
no firmware change. It is also the riskiest thing in the opcode set, for the
reasons listed above.

### What the measurement said

Measured on a live board (firmware 3.0.6, all four lanes loaded) on 2026-09-01,
with `IFS_JOG_SELECTOR` driving `F30` and the selector driver's standstill bit
reporting arrival. Ten timed moves at three distances, least-squares fitted:

| distance | measured |
|---|---|
| 2048 steps | 2.46, 2.40, 2.30, 2.55 s |
| 8192 steps | 4.41 s |
| 16384 steps | 6.93, 7.03, 6.83, 6.81, 7.02 s |

Arrival detection costs a fixed ~1.8 s (three `F63` confirmations at ~0.6 s
each), so the absolute times overstate the move. That offset is constant, which
is exactly why several distances were timed rather than one: the fit separates
it out. Every residual falls within +/-0.13 s of a straight line, which is
itself the evidence that the rate is constant - there is no ramp.

```
detection lag  : 1.79 s
MEASURED       : 3190 steps/s
derived        : 3205 steps/s      ratio 0.995
```

**Within 0.5%.** The whole chain holds - TIM6 at `ARR` 155, the APB1 prescaler
of 4, `SystemCoreClock` carrying HCLK, one STEP edge per interrupt.

The geometry is confirmed too, and visually rather than by inference: a
16384-step jog takes the cam **all the way around through every lane**, once.
So the eight-stop ladder is the real mechanism, not a reading of step numbers
that happened to fit.

What that makes true, in pure motion time:

| move | steps | time |
|---|---|---|
| clamp to release, one lane | 2048 | 0.64 s |
| slot to slot | 4096 | 1.28 s |
| one full revolution | 16384 | 5.14 s |
| worst-case `F24`, re-home included | 32768 | 10.3 s |

And measured end to end through klipper, `IFS_CLAMP CHANNEL=3` took **7.38 s** -
for a move the turret could have made in 1.28 s had it known where it was.

To re-run it, `tools/ifs/ifs_selector_timing.py` does the same thing standalone
on the printer (klipper stopped), or from klipper:

```
IFS_JOG_SELECTOR POSITION=0        # a known start
IFS_JOG_SELECTOR POSITION=2048     # short leg
IFS_JOG_SELECTOR POSITION=0
IFS_JOG_SELECTOR POSITION=16384    # long leg; subtract to cancel the lag
```

## A companion on the wire: the IFS Jacker

Everything above is the IFS board's own firmware. A machine with an IFS Jacker
(https://github.com/ninjamida/ifs-jacker) carries a pass-through on this
serial link: F traffic goes through untouched, the Jacker answers Z opcodes a
bare board cannot, and firmware 3.0 and later appends its peripherals' state
to every `F13` payload as `p<id>_<param>` tuples after the board's own fields.
The status parser ignores fields it does not know, so the tuples change
nothing for a machine without one. Detection and the `IFSJ_*` commands live in
the module: [IFS_MODULE.md](IFS_MODULE.md#companion-the-ifs-jacker).

## Versioning

A `F112 ok. yes.` variant is accepted by some drivers and **firmware 3.0.6 never
emits it**. Responses differ across IFS firmware
revisions. The missing `channel_count` field is a second instance of the same
thing. Probe `F19` and branch on the version rather than assuming this table
holds for every board.

**3.0.7, externally checked:** the Wire section and response prefix rules carry
over unchanged (independent tester, zmod source + hardware); the `0xFF` commit
byte is not needed there either; and the empty-channel refusal documented above
is gone - feed/unload opcodes run with nothing clamped. The `F19` probe remains
the right place to branch when behavior differs.

## Ground truth

Two independent sessions on a real AD5X, 2026-08-28. Everything marked measured
in this document comes from one of them.

### 1. Passive capture, stock boot

`strace` attached to `firmwareExe`'s `/dev/ttyS4` fd. Nothing else opened the
port, so no bytes were taken from the stock UI's own session.

```bash
PID=$(ps | grep '[f]irmwareExe' | awk '{print $1}')
FD=$(for f in /proc/$PID/fd/*; do
        case "$(readlink $f)" in */ttyS4) echo "${f##*/}";; esac; done)
strace -f -tt -s 512 -y -e trace=read,write -e trace-fds="$FD" -p "$PID" -o capture.log
```

Two traps when reading such a capture. `firmwareExe` reads **one byte per
`read()`**, so a reply is one syscall per byte and must be reassembled. And an
interrupted syscall is split across two lines as `read <unfinished ...>`; a parser
that ignores those reports the reply short by a byte, which looks exactly like the
board dropping data. It is not.

### 2. Direct probe, Forge-X boot

Under Forge-X the stock UI is stopped and `uart4` sits at `tx:0 rx:0`, so the port
is **free** and the board can be driven directly - no stock boot, no heat. This is
how the framing A/B, the `F19` probe, the full read-only opcode sweep and the
`F11` capture were done. `pyserial` 3.4 is present at
`/usr/prog/Python-3.8.2/bin/python3` (needs `LD_LIBRARY_PATH` - lift it from the
running klippy's `/proc/<pid>/environ`).

The read-only half is `tools/ifs/ifs_probe.py`, which is safe to re-run on any
board: it refuses every actuator and the four undocumented opcodes, and brackets
the sweep with an `F13` snapshot. **Run it against a different board or firmware
revision before trusting the tables above** - they are one board at 3.0.6.

Motion was tested separately by hand: clamp (`F24 C2`), retract 20 mm at
600 mm/min (`F11 C2 L20 S600`), feed the same distance back, release (`F39 C2`),
with `F112` between phases and a release in the failure path. Filament ended
where it started.

### The state encoding, confirmed on hardware

Every channelled activity was observed on channel 2, and each matches
base + 11*(channel-1) exactly:

| observed | decodes as | after |
|---:|---|---|
| 18 | clamped, channel 2 | `F24 C2` |
| 22 | loading, channel 2 | `F10 C2 ...` |
| 23 | unclamping, channel 2 | `F39 C2` |
| 26 | unloading, channel 2 | `F11 C2 ...` |

`L` is millimetres and `S` is mm/min, confirmed by timing: `L600 S1200` took
exactly 30 s, and 600/1200 = 0.5 min.

### The insert sequence, observed

Filament pushed into an empty channel 2, driven by the stock UI:

```
board reports  silk 9->11, insert=2      board notices by itself
F24 C2         -> F24 ok. chan 2.        clamp        state=18
F10 C2 L600 S1200 -> ... channel 2 feeding.           state=22
               (30 s: 600 mm at 1200 mm/min)
F112           -> F112 ok.               stop
F23 C2         -> F23 ok. chan 2.
F39 C2         -> ... channel 2 release.              state=23, insert=0
```

`ffs_channels_insert` went 2 -> 0 across this sequence.

### `F23` is what clears the insert bit, and the bit is a REQUEST

In the driven sequence `F112`, `F23` and `F39` land within 350 ms of each other,
which leaves the clearer ambiguous. A run where two lanes failed to thread
separated them - the failing lanes never reached their `F23`:

| lane | opcodes sent | insert bit afterwards |
|---|---|---|
| 4 | `F112`, **`F23`**, `F39` | **cleared** |
| 2 | `F112`, `F39` (threading failed before its `F23`) | still set |
| 1 | `F112`, `F39` (same) | still set |

`F112` and `F39` both happened in the failing cases and left the bit alone, so
**`F23` is the clearer**, as the naming suggested.

That also settles what the field *means*. A set bit is the board asking to have
that lane threaded, and `F23` is the acknowledgement - so reading it as "channels
that are inserted" gets it backwards. This repo's status object publishes it as
`pending_insert_channels`.

## Not yet verified

- **Whether a full unload differs from a bare `F11`.** `F11` itself is confirmed
  on the wire (`F11 C2 L20 S600` -> `F11 ok. FFS channel 2 exiting.`, state 26),
  but that was driven by hand. The stock UI's complete unload flow - opcode order,
  lengths, and how it coordinates with the extruder - has not been captured,
  because an unload needs filament actually loaded to the nozzle.
- **What exactly `F40`'s stall counters count.** They are per-lane and they go
  DOWN as well as up, so they are not a lifetime total. Three readings in one
  session - `[9, 58, 0, 119]`, then `[428, 467, 0, 69]`, then
  `[195, 161, 0, 488]` - each moved only for the lanes that had just been asked
  to move, and a lane that has never held filament read 0 throughout. That is
  consistent with "how much of the most recent commanded move did not happen":
  the 428 and 467 followed 600 mm feeds that barely moved, the 69 followed the
  one feed that worked, and the 488 followed a 1000 mm eject of which only about
  676 mm had filament left to pull. Consistent with, not established.
- ~~The selector step rate is derived, not timed.~~ **Settled**: measured at
  3190 steps/s, 0.5% off the derivation. See "What the measurement said".
- ~~`F30` has not been sent to a board.~~ **Settled**: sent on hardware
  2026-09-01. It moves the selector, it does not disturb loaded filament, and
  `F15 C` does clear the state-129 park - the board came straight back to
  `ready` and took a normal `F24` afterwards.
- **What `F30` does to a lane it stops between.** Every position used here was
  one of the eight stops. A target part-way between two of them is accepted by
  the firmware and has not been tried; whether the cam rests somewhere harmless
  is unknown.
- **Whether the position counter survives anything but a warm board.** It lives
  in RAM at `0x200001E0` and nothing reads it back, so a host that tracked
  position would be trusting a number it cannot verify. That is the open risk
  in any `F30` fast path.
- Whether the trailing space and the `0xFF` byte matter to any **other** IFS
  firmware revision. 3.0.1 and 3.0.5 also ignore the `0xFF` (externally
  confirmed by GhostTypes). On 3.0.6 neither is needed: the stock FlashForge host
  sends the byte ~200 ms after every command, the board accepts commands with
  and without it, and the byte costs a hard 200 ms of delay per command when a
  driver sends it. On 3.0.7 it does not matter either (externally confirmed).
- The stock UI's complete unload flow - opcode order, lengths, extruder
  coordination - is still uncaptured (an unload needs filament loaded to the
  nozzle to drive by hand). ninjamida (IFS Jacker's author) points to ghzserg
  (zmod's author) as having answers; worth a capture session before trusting
  our inferred order.

## Host pacing (reference)

Three known driver cadences, for anyone tuning poll intervals: the stock host
polls every 200 ms idle and 1000 ms printing (per the community wiki's firmware
analysis); zmod sends everything - polls and commands alike - on a 0.4 s tick
plus execution delay, and that keeps up with a print. This module polls every
`poll_interval` (default 1.0 s) at idle and drops to 0.2 s while a feed or
unload is in flight and being watched.
