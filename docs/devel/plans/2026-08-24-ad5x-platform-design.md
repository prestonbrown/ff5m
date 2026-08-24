# Forge-X for AD5X: Platform Design

Status: draft, 2026-08-24
Branch: `feat/ad5x-platform`
Goal: a flashable AD5X firmware image built on our own platform layer, with IFS.

## 1. What we are building

Forge-X currently targets the FlashForge Adventurer 5M (Pro) only: ARM, one hardware
profile, no platform abstraction anywhere in the tree. This project adds the AD5X, which
differs in CPU architecture, userland, serial map, and headline feature (the 4-slot IFS).

The end artifact is a USB-installable `.tgz` that the stock FlashForge updater accepts,
producing a printer that runs Klipper, Moonraker, a HelixScreen UI, the Forge-X macro and
settings layer, and working multi-material printing.

### Non-goals for this project

- AD5M feature parity. Forge-X features that depend on AD5M hardware are out of scope
  unless they port for free.
- A public release. This targets our own bench first. Install UX, upgrade paths from
  ZMOD, and support for the full matrix of stock firmware versions come later.
- Authoring MCU firmware. The mainboard, eboard, and IFS controller keep their stock
  FlashForge blobs. We are a host-side mod.

### Explicitly in scope, by decision

- **Clean-room platform layer.** We build our own MIPS userland rather than inheriting
  ZMOD's ~1 GB chroot. See section 4 for why this is the long pole.
- **IFS in v1.** The AD5X without multi-material is not the AD5X, so the port of the
  filament system is part of the first flashable build rather than a follow-up.

## 2. Target facts (measured, not assumed)

All of the following was verified on our own AD5X at 192.168.1.66 on 2026-08-24.

| Property | Value |
|---|---|
| CPU | MIPS32r5, little-endian, 2 cores |
| Kernel | 5.10.186 |
| RAM | 485 MB total, ~386 MB available at idle |
| `/` | 12.5 MB read-only squashfs, 100% full |
| `/usr/prog` | `mmcblk0p6`, 973 MB, 85% used |
| `/usr/data` | `mmcblk0p7`, 5.5 GB, 22% used |
| Klipper host | runs on `/`, not chrooted (`/proc/<pid>/root -> /`) |
| Klipper Python | FlashForge's own **3.8.2** at `/usr/prog/Python-3.8.2` |
| Klipper tree | `/usr/prog/klipper`, config `/usr/data/config/printer.cfg` |
| Moonraker | ZMOD runs it **inside** its chroot on Python **3.12.9** |
| IFS transport | `/dev/ttyS4`, 115200 8N1 |
| eboard | `/dev/ttyS5` (AD5M uses `/dev/ttyS1`) |
| Install vector | stock updater runs `flashforge_init.sh AD5X 0026` off USB vfat |
| Toolchain in hand | `mipsel-buildroot-linux-gnu-` GCC 13.3.0, glibc 2.40 |

Two of these drive most of the design:

**Klipper runs on the host with FlashForge's Python 3.8.2.** We do not need a new
interpreter for Klipper, and our klippy code must stay 3.8-compatible. Verified: Forge-X's
`mod_params.py` loads and runs there unmodified.

**Moonraker needs Python 3.10+** (`server.py` guards on `sys.version_info < (3, 10)`).
Nothing on the stock box satisfies that. This single requirement is the entire reason a
separate userland exists, in ZMOD's design and in ours.

## 3. Architecture

Four layers, with the boundary between 1 and 2 being the one that must stay clean.

```
  Layer 3  Application   Forge-X macros, configs, mod_params, display modes
  Layer 2  Klipper       stock klippy + merged Forge-X patches + our extras (incl. IFS)
  Layer 1  Platform      our MIPS userland: Python 3.12 + Moonraker, boot hooks, recovery
  Layer 0  Stock         FlashForge kernel, rootfs, Python 3.8.2, MCU blobs (untouched)
```

Layer 0 is never modified. Every install is additive to `/usr/data` plus hooks, so a
factory restore always recovers the printer.

The Layer 1/2 boundary is the important one: Layer 2 and 3 must not know how Layer 1 was
built. If we later replace our userland, or if someone runs this on a different base,
nothing above should notice. Concretely this means no code above Layer 1 may reference a
chroot path, an interpreter path, or a Buildroot detail. Layer 1 exposes exactly three
things: a Python 3.12 environment that can run Moonraker, a service start/stop interface,
and the log and config directories.

## 4. Layer 1: the platform layer (the long pole)

### What it must provide

1. A Python 3.12 runtime capable of running Moonraker and its dependency set.
2. Service supervision for Moonraker, the web server, and the screen.
3. Boot integration with the stock firmware.
4. A recovery path when we break something.

### Approach

Buildroot, producing both a cross toolchain and a minimal rootfs for MIPS32r5 glibc.

We already know the exact target parameters because a working Buildroot toolchain for
this printer exists: `mipsel-buildroot-linux-gnu-` GCC 13.3.0 against glibc 2.40, with
OpenSSL 3.4.1 and zlib 1.3.1. That toolchain is currently sourced from ghzserg (ZMOD's
author) and mirrored into HelixScreen's releases. **Replacing it with our own Buildroot
output is part of what "clean-room" means here** and is the first milestone, because
until it exists every other layer is still standing on ZMOD.

Deliberate size target: ZMOD's chroot is ~1 GB. Ours should be a small fraction of that.
We need Python 3.12, Moonraker's dependencies, a web server, and busybox. We do not need
mido, a MIDI stack, audio libraries, a shaper toolchain, or twelve locales. On a box with
485 MB of RAM and a 973 MB partition, that difference is a real feature and not vanity.

### Boot integration

Stock provides two hook points, both already proven by ZMOD:

- `/usr/prog/app_startup.sh` for early setup and bind mounts
- `/usr/prog/klipper/start.sh` for pre-Klipper configuration

We take the same two hooks. We also adopt one specific ZMOD trick deliberately: bind-mount
`/opt` to the writable data partition so that `/opt/config/...` resolves. Forge-X hardcodes
that prefix in 14 places, and matching it means the application layer ports without a
path-rewriting pass. This is a compatibility decision, documented as such, not an accident.

### Install and recovery

The stock updater is the install vector. A `.tgz` on a vfat USB stick containing
`flashforge_init.sh`, which the firmware invokes as `flashforge_init.sh AD5X 0026`. It
verifies `md5sum.list`, paints progress to `/dev/fb0`, and reboots via sysrq.

Recovery has three tiers:

1. **Boot skip.** A flag file causes our init to bail and boot stock, as Forge-X does on
   AD5M with `BOOT_SKIP_F`.
2. **Uninstall image.** A second USB `.tgz` that removes us.
3. **Factory restore.** `AD5X-1.1.7-1.1.0-3.0.6-20250912-Factory.tgz`. Always works because
   Layer 0 is untouched.

Tier 1 must exist before we ever flash tier-0 code, because the failure mode we care about
is a printer that boots to a black screen with no network.

One hard-won install gotcha to encode in the docs: **leave the USB stick out after
installing.** The installer deletes its own `.tgz` on success, but if the stick is still
present at next boot the installer re-triggers and dies on a busy mount, which looks
exactly like a failed install.

## 5. Layer 2: Klipper

### Patch merge, measured

Forge-X ships 9 replacement klippy extras plus `gcode.py` and `configfile.py`. A three-way
merge (base = FlashForge's AD5M GPL drop, theirs = Forge-X, ours = AD5X) gives:

| File | Result |
|---|---|
| `gcode_move.py` | clean, AD5M/AD5X stock byte-identical |
| `led.py` | clean, byte-identical |
| `temperature_sensor.py` | clean, byte-identical; Forge-X's +90 lines apply as-is |
| `buttons.py` | clean, 33 lines of AD5X drift with no overlap |
| `configfile.py` | clean, 37 lines of drift, no overlap |
| `gcode.py` | clean, 17 lines of drift, no overlap |
| `statistics.py` | 1 conflict, trivial (Forge-X is a strict superset) |
| `resonance_tester.py` | 3 conflicts, shaper API |
| `shaper_calibrate.py` | 4 conflicts, shaper API |
| `virtual_sdcard.py` | **unknown**, see below |
| `gcode_shell_command.py` | **unknown**, no stock base exists |

Six of nine assessable files merge with zero conflicts.

### The shaper API problem

`resonance_tester.py` and `shaper_calibrate.py` are two halves of one interface, and AD5X
ships a **newer** `shaper_calibrate` than Forge-X was written against:

```python
# AD5X:    find_best_shaper(data, max_smoothing=..., scv=..., max_freq=..., logger=...)
# Forge-X: find_best_shaper(data, max_smoothing, gcmd.respond_info, scv=...)
```

Keyword versus positional, and AD5X has `max_freq` which Forge-X does not know about.
Merged carelessly this raises `TypeError` at calibration time rather than at load time, so
it passes startup and fails only when a user runs input shaping.

Resolution: keep AD5X's newer API, port Forge-X's additions onto it, never the reverse.
These two files must be merged together and tested together.

### Two files we cannot yet assess

`virtual_sdcard.py` and `gcode_shell_command.py` on our rig are **ZMOD's**, not stock,
confirmed by md5 against `/usr/data/config/mod/.shell/`. ZMOD overwrites klippy extras in
place with no backup and `/usr/prog/klipper` is not a git repo, so the running printer
cannot tell us what stock looks like.

`virtual_sdcard.py` matters most: it is the print path, and both mods modify it heavily.
Getting true stock requires extracting the factory image. **This is a prerequisite for
Milestone 2** and is the first task in it.

## 6. Layer 2b: IFS

### What the protocol actually is

Good news, from reading ZMOD's `zmod_ifs.py` (1389 lines, Apache-2.0):

- Transport is `/dev/ttyS4` at 115200 8N1, imports are stdlib plus pyserial only.
- The wire format is an **ASCII line protocol**: write `"F13\r\n"`, write a single `0xFF`
  byte, read a line back. No CRC, no binary framing, no checksums.
- Klipper coupling is four objects: `gcode`, `query_adc`,
  `temperature_sensor filamentValue`, and optionally `zmod` / `zmod_color`.
- ZMOD **flashes the stock vendor `ifs.hex`** via `IFSCommand`. They did not author IFS
  firmware and neither will we.

So the protocol is trivial. The value is the knowledge encoded around it: the F-command
set (F10/F11/F13/F15/F18/F23/F24/F39/F112), the status deltas, the return codes, and the
stall/silk/extruder-sensor retry semantics. That is genuine reverse engineering that
exists nowhere else.

### What we will not copy

ZMOD's implementation has three properties we must not inherit:

1. A background thread polling serial at 5 Hz **continuously, even idle**, with a
   `time.sleep(0.2)` per command, on a 2-core MIPS box. This is precisely the host-load
   class that triggers "Timer Too Close" on this platform, which is our open incident
   family on AD5X.
2. `send_command_and_wait` busy-waits in `reactor.pause()` slices and, on timeout, **runs
   gcode from inside the error path** (`_ENABLE_SENSOR`, `IFS_F112`, `IFS_F18`) before
   raising.
3. A single mutable `self._command` slot shared with the reader thread, guarded only by an
   incrementing ID. No queue, so commands can be dropped.

### Our design

A clean `ifs.py` klippy extra in two parts:

- **Transport.** A queued serial worker. Idle costs nothing: no polling when no command is
  outstanding and no sensor subscription is active. All I/O off the reactor thread, results
  delivered back through the reactor.
- **State machine.** The load/unload/cut/purge sequences, ported from ZMOD's semantics with
  the retry logic intact, but with no gcode execution from error paths.

`zmod_color.py` (1725 lines) is **not ported**. It does slicer colour parsing, tool mapping,
auto-assignment, native-screen dialog driving, Moonraker HTTP calls from inside a klippy
extra, and carries its own translation table. HelixScreen already models most of that
properly in `AmsBackend`. Our klippy side stays thin and the UI owns presentation.

### Licensing

ZMOD's `_mod/` tree, including `zmod_ifs.py`, is **Apache-2.0**. Forge-X is GPL-3.0.
Apache-2.0 into GPL-3.0 is compatible one-way, so we may incorporate the IFS work with
attribution and a NOTICE. The reverse would not be allowed. No blocker, but attribution
must be explicit and correct: this is ghzserg's reverse engineering and the file must say so.

## 7. Layer 3: application

Mostly a port, since Forge-X's application layer is the part we actually want.

- **`mod_params`.** Verified working on AD5X unmodified: loads under Python 3.8.2,
  registers as a Klipper object, `get_status()` returns all 40 parameters through Moonraker,
  and `SET_MOD` persists correctly typed values to the configparser INI. No work needed.
- **Macros and configs.** Forge-X's `base.cfg` and friends are AD5M-shaped. Kinematics,
  sensors, fan names, and the clear/purge macros need an AD5X profile. This is the bulk of
  the porting labour and it is unglamorous.
- **Display modes.** `stock` is meaningless for us early on. HelixScreen is the target;
  headless is the fallback. The HELIX display mode is already written and is in flight
  upstream as PR #74.

### The platform abstraction Forge-X does not have

Forge-X hardcodes `Adventurer5M*.json`, `Adventurer5M*.tgz`, and "AD5M" in the motd, and
has no `uname -m` anywhere. Adding a second platform means introducing that abstraction.

This is worth doing **as its own upstream-shaped change** rather than as a side effect,
because it is a genuine cleanup for AD5M on its own merits and it is the piece most likely
to be accepted upstream. It also determines whether this work can ever merge into Forge-X
proper rather than living in our fork forever.

## 8. Milestones

Each milestone ends in something we can put on the bench.

**M1. Our own userland.** Buildroot config producing a MIPS32r5 glibc toolchain and a
minimal rootfs with Python 3.12 and Moonraker's dependencies. Success: Moonraker starts
inside it on the AD5X and answers `/printer/info`. This displaces the ghzserg toolchain
and is the milestone that makes "clean-room" true.

**M2. Klipper layer.** Extract the factory image for true stock `virtual_sdcard.py` and
`gcode_shell_command.py`. Complete the three-way merges, resolving the shaper API pair
together. Success: klippy starts with all merged patches and a print completes.

**M3. Boot and recovery.** Our own init hooks, boot-skip flag, and uninstall image, plus
the USB `.tgz` the stock updater accepts. Success: a flash from USB that comes up on our
stack, and a boot-skip that reliably falls back to stock. **This is the first genuinely
flashable artifact.**

**M4. Application layer.** AD5X macro and config profile, `mod_params` wired, HelixScreen
as the display. Success: a normal single-colour print start to finish, driven from the panel.

**M5. IFS.** Transport, then state machine, then the load/unload/change sequences. Success:
a four-colour print completes without intervention.

M1 through M3 are sequential. M4 can overlap M3. M5 is the long tail and its real cost is
print hours, not code.

## 9. Risks

**The userland is the schedule.** M1 is the piece with the least existing art and the most
ways to go wrong: a Buildroot config that produces a working glibc 2.40 MIPS32r5 rootfs
small enough to be worth the effort. Everything else has a reference implementation to read.

**TTC on host load.** This platform shuts down the MCU when the host is busy during motion.
Forge-X's own regression harness had to stop taking screenshots during toolhead movement for
this reason, and their FAQ concedes the MCU-overload class is not fixable host-side. Our IFS
implementation is host code that runs during motion, so its idle and polling behaviour is a
correctness concern, not a performance nicety.

**Two files still unknown.** `virtual_sdcard.py` is the print path. If AD5X stock diverges
badly from AD5M stock there, the merge could be materially harder than the six clean files
suggest. We will not know until the factory image is extracted, which is why that is task
one of M2 rather than something to discover late.

**Stock firmware drift.** FlashForge publishes no AD5X source and no changelogs since 2024,
and pushes forced OTAs. Every stock release is a potential silent break. This is the
permanent tax and it is what ended klipper-mod. Mitigation is pinning a known-good stock
base and testing new ones deliberately rather than tracking them.

**Bench dependency.** We have exactly one AD5X and it is shared with other work. Anything
that touches the boot chain risks making it unavailable. Tier-1 recovery must exist before
the first boot-chain flash, and we should be honest that a bricked bench blocks everything.

## 10. Open questions

1. Do we pin a stock base version now? ZMOD supports discrete versions to 3.1.0 and the
   factory image is 1.1.7. Our rig is on a factory-restored 1.1.7.
2. Does this ultimately target upstream Forge-X or stay a fork? It changes how much effort
   goes into the platform abstraction in section 7. Worth asking DrA1ex once PRs #73 and
   #74 have landed and there is a working relationship to ask across.
3. Do we want the stock screen to remain usable at all, or is HelixScreen the only
   supported display from day one?
