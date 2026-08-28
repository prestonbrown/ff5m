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
> do not filter runs by "looks like a word" - that drops `F15 ok.` and `F18 ok`,
> which is how a first pass reported 20 opcodes instead of 32.

## Wire

| | |
|---|---|
| Port | `/dev/ttyS4` |
| Baud | 115200, 8N1 |
| Read timeout | 0.2 s |
| Request | `<command>\r\n`, then after ~200 ms a bare `0xFF` byte |
| Reply | one or more `\r\n`-terminated lines (see framing) |

The trailing `0xFF` is required - it appears to commit the command. Any
reimplementation must send it.

`/proc/tty/driver` shows `uart4` at `tx:0 rx:0` under a Forge-X bring-up, because
we stop the stock UI and ship no IFS driver. Under stock, `firmwareExe` owns it.

## Response framing

Three rules, all read straight off the firmware's format strings, and all three
missing from any driver:

**1. Every response echoes the request opcode: `F<n> ok.`** Not one string in the
image breaks this. That is free request/response correlation on the wire - a
reply can be matched to its request without driver-side bookkeeping, and a stale
or unsolicited line can be discarded instead of parsed as the answer to whatever
was asked last. `zmod_ifs.py` does not use it; it tracks its own `#<id>` slot
instead.

**2. `ok.` is an echo, not a status.** `F10 ok. FFS not ready.` is a *failure*.
Success has to be read from the payload after the prefix, never from `ok.`

**3. The separator after `ok.` is inconsistent.** `F10`-`F24`, `F39` emit
`"F13 ok. FFS_state:"` with a space; `F40`-`F64` emit `"F40 ok.stall count:"`
with none. Split on the `F<n> ok.` prefix and strip - do not split on `"ok. "`.

Most responses also carry a **trailing space** before the CRLF. Strip.

### `F21` returns three lines

```
F21 ok. \r\n silk: %d %d %d %d \r\n stall: %d %d %d %d
```

A transport that does one `readline()` per command reads `F21 ok.` and leaves two
lines in the buffer, which then answer the *next* two requests. Every later poll
is off by one, silently. Read until the response is complete, not until the first
newline. No other opcode does this.

## Commands

`C` selects a channel (1-4), `L` a length in mm, `S` a speed in mm/min.

| Command | Arguments | Meaning |
|---------|-----------|---------|
| `F10 C<n> L<mm> S<speed>` | channel, length, speed | feed filament |
| `F11 C<n> L<mm> S<speed>` | channel, length, speed | retract filament |
| `F13` | none | status poll |
| `F15 C` | literal `C` | reset driver |
| `F18` | none | unclamp all channels |
| `F23 C<n>` | channel | mark filament inserted |
| `F24 C<n>` | channel | clamp channel |
| `F39 C<n>` | channel | release/unclamp channel |
| `F112` | none | stop feeding |

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
- `ffs_channels_insert` - consumed as a bit length
- `chan` - active channel, 0 = none

### Where `channel_count` comes from: nowhere, on this firmware

`zmod_ifs.py` regexes `channel_count:\s*(\d+)` out of the `F13` line. **That field
is not in 3.0.6's format string**, so on this board the regex never matches and
the value stays 0. It is presumably a newer IFS revision's field.

ZMOD's actual channel count is neither probed nor parsed: it is the `color_limit`
config option, `config.getint('color_limit', 4)`. Every install is hardcoded to
four by default. `F19` is the real probe - see below.

### State values

A base plus a per-channel stride of 11 (`FFS_STATUS_DELTA`), which is why zmod's
comments read "18, 29, 40" / "22, 33, 44" / "26, 37, 48" / "23, 34, 45":

| Base | Meaning |
|-----:|---------|
| 3 | polling channels |
| 5 | ready |
| 7 | channel clamped |
| 11 | loading |
| 12 | unclamping |
| 15 | unloading |
| 127 | driver error |

### Return codes (driver-side, from `zmod_ifs.py`)

`0` ok · `1` extruder sensor tripped · `2` filament sensor tripped · `3` stall ·
`4` timeout waiting for a status · `5` program exit · `6` retry

## Every opcode the firmware answers

32 opcodes, 58 distinct response strings. **ZMOD uses 9.** Responses below are
verbatim, including the `F<n> ok.` prefix (`%d`/`%02x` are the firmware's own
format specifiers).

| Opcode | Response | Used by ZMOD |
|--------|----------|:---:|
| `F10` | `F10 ok. FFS channel N feeding.` / `... FFS channel not exist.` / `... FFS not ready.` / `... No channel selected.` | yes |
| `F11` | `F11 ok. FFS channel N exiting.` + same error set | yes |
| `F12` | `F12 ok. %d %d %d %d` | no |
| `F13` | the status line above | yes |
| `F14` | `F14 ok. stall: %d %d %d %d` | no |
| `F15` | `F15 ok.` | yes |
| `F18` | `F18 ok` *(no trailing period)* | yes |
| `F19` | `F19 ok. four color. version: 3.0.6` | **no** |
| `F20` | `F20 ok.` | no |
| `F21` | `F21 ok.` + ` silk: %d %d %d %d` + ` stall: %d %d %d %d` **(3 lines)** | no |
| `F22` | `F22 ok. ffs_channels_insert: %d` | no |
| `F23` | `F23 ok. chan N.` / `F23 ok. no chan.` | yes |
| `F24` | `F24 ok. chan N.` / `... FFS channel not exist.` / `... No channel selected.` | yes |
| `F30` | `F30 ok.` | no |
| `F39` | `F39 ok. FFS channel N release.` + error set | yes |
| `F40` | `F40 ok.stall count: C1: %d C2: %d C3: %d C4: %d` | no |
| `F41` | `F41 ok.GCONF: %02x%02x%02x%02x` | no |
| `F42` | `F42 ok.stepper_motor: %d stepper_motor_irun: %d` | no |
| `F43` | `F43 ok.` | no |
| `F44` | `F44 ok.DRV_STATUS: %02x%02x%02x%02x` | no |
| `F45` | `F45 ok.GSTAT: %02x%02x%02x%02x` | no |
| `F50`-`F54` | `GCONF`,`GSTAT`,`CHOPCONF`,`DRV_STATUS`,`PWMCONF` - driver 1 | no |
| `F60`-`F64` | the same five - driver 2 | no |
| `F112` | `F112 ok.` | yes |

## What the unused two-thirds buys us

- **`F19` is a capability probe.** `F19 ok. four color. version: 3.0.6` gives
  channel count *and* firmware version. Note it is a **literal** - no format
  specifiers - so both the count word and the version are baked into each
  firmware build, and a different board answers with its own literal. Every
  driver today assumes four channels; this asks.
- **`F14`, `F21`, `F22`, `F40`** are cheaper/narrower reads than the full `F13`
  line - `F21` returns silk+stall together, `F40` returns cumulative stall counts.
- **`F41`/`F44`/`F45` and `F50`-`F54` / `F60`-`F64`** expose both TMC drivers'
  `GCONF`, `GSTAT`, `CHOPCONF`, `DRV_STATUS`, `PWMCONF`. Real motor diagnostics -
  overtemperature, open load, short detection - none of which reaches any UI today.
- `F12`, `F20`, `F30`, `F43` acknowledge but reveal nothing about their effect.
  Do not send them blind; they may actuate.

## Versioning

`zmod_ifs.py` accepts `F112 ok. yes.` as well as `F112 ok.`, and **firmware 3.0.6
never emits the `yes.` variant**. So responses differ across IFS firmware
revisions. The missing `channel_count` field is a second instance of the same
thing. Probe `F19` and branch on the version rather than assuming this table
holds for every board.

## Not yet verified

- No live capture. Everything here is the firmware's own format strings plus
  `zmod_ifs.py`'s command construction. A passive sniff of `/dev/ttyS4` at 115200
  under a **stock** boot, while driving the IFS from the stock UI, would confirm
  the cadence and might reveal opcodes with no format string.
- The effect of `F12`, `F20`, `F30`, `F43` is unknown.
- Whether the ~200 ms gap before the `0xFF` commit byte is required, or merely
  what ZMOD happens to do. It costs a hard 200 ms per command.
