# The Klipper layer on AD5X

Forge-X does not run its own Klipper. It patches the vendor's, by shipping whole
replacement files and symlinking them over the stock tree at boot. AD5X needs the
same treatment against a different vendor tree, and this document is what we
learned doing it and how to check the result.

`tools/klipper-merge/README.md` covers how the files are derived and why each
conflict was resolved the way it was. This is the layer around that: what the
vendor's tree actually looks like, what is verified and how, and how to bring the
thing up on a printer.

## What FlashForge ships

**The AD5X image carries Klipper; the AD5M image does not.** On AD5X the host is
`other/klippy.tar` inside the factory `.tgz`. On AD5M there is no `klippy`
anywhere in the image - not in `boot.img`, `control-*`, `kernel-*`, `library-*`
or `software-*` - because Klipper lives in that printer's rootfs and is updated
by another route. Anyone reaching for "just diff the two factory images" will
find half of it missing.

Nothing in a FlashForge image is compressed. `.tgz` and `.tar.xz` are both plain
uncompressed tar, at every level of nesting. `tar xzf` and `tar xJf` both fail.

**The Klipper tree does not move between stock releases.** Every file in
`klippy/` is byte-identical between 1.1.7 (2025-09-12) and 1.2.3 (2026-02-05).
That is the whole tree, not just the eleven files we patch. It is the reason
tracking current stock rather than pinning to a factory image costs nothing at
this layer, and it is worth re-checking whenever a new stock release appears -
`tools/klipper-merge/SOURCES.md` has the extraction recipe.

## Where AD5X diverges from AD5M

Of the eleven files Forge-X patches, stock is byte-identical on both platforms
for three (`gcode_move.py`, `led.py`, `temperature_sensor.py`) and absent on both
for one (`gcode_shell_command.py`, a community extra neither vendor ships). Those
four need no AD5X copy. The other seven do.

Two divergences matter beyond the merge:

**`virtual_sdcard.py` is the tool-change path.** On AD5X it is not only the print
path: stock watches the gcode stream for `T0` through `T15` while printing and
drives `load_channel`, `print_channel`, `change_filament` and `enable_ffm` from
it, exposing `channel` and `refuelling` through `get_status`. That is the hook
the four-colour filament system hangs off. It has to survive every merge intact,
which is why `test/python/test_ad5x_klipper_patches.py` asserts the state, the
`SDCARD_*` commands and the sixteen-tool list directly rather than trusting the
diff.

**The shaper API is a matched pair, and AD5X's is newer.**
`resonance_tester.py` and `shaper_calibrate.py` are two halves of one interface,
and AD5X ships a later revision than Forge-X was written against. Merged
carelessly this does *not* fail at load time. It fails when a user runs
`SHAPER_CALIBRATE`, and in the worst case it does not fail at all: Forge-X's
older positional call fits AD5X's newer signature and silently binds
`max_smoothing` to `shapers` and the logger to `damping_ratio`. Those two files
are merged together, tested together, and the test checks argument binding by
name rather than by count.

## What is on the rig is not stock

A printer running another mod cannot tell you what stock looks like. On our AD5X,
`virtual_sdcard.py` and `gcode_shell_command.py` are byte-identical to ZMOD
1.7.1's, which overwrites klippy extras in place with no backup and where
`/usr/prog/klipper` is not a git repo. The other nine files on that rig *are*
true stock, but there was no way to know which was which without the factory
image. Pull the base from the image, never from a modded printer.

## How it is verified

Four gates, each catching something the others do not.

    tools/klipper-merge/merge.sh          # the derivation still holds
    sh test/run.sh                        # overlay resolution, plus the M1 suites
    python3 -m pytest test/python/        # the behaviours the merge can lose

`merge.sh` proves the AD5X files are the merge we think they are, and fires when
Forge-X's patches or FlashForge's stock move underneath a hand-resolved file.

The pytest suite proves the merge is *correct*, which `merge.sh` cannot. Both
ways this merge goes wrong are silent, and one of them already happened:
`statistics.py` took Forge-X's `disabled` option without the `if not
self.disabled:` that acts on it, because AD5X had reordered the two lines beside
it. It merged clean, klippy started, and the option did nothing.

The fourth gate is a klippy bring-up off the printer, below.

## Bringing klippy up without a printer

Klippy can be run on a workstation far enough to prove that every patched file
loads against the real stock config. `_read_config()` parses the config, calls
`load_config` on every section, and validates that no option went unread - all
before it touches the MCU. So a run that dies on `mcu 'mcu': Unable to connect`
has already exercised the entire patched stack.

    # 1. Stock tree and stock config, from the factory image (see SOURCES.md).
    tar xf AD5X-1.1.7-...-Factory.tgz ./other/klippy.tar ./other/printer.cfg
    tar xf AD5X-1.1.7-...-Factory.tgz -O ./software-1.1.7.tar.xz > sw.tar
    tar xf sw.tar ./printer.base.cfg

    # 2. Apply the patches the way .shell/S00init does: shared set, then the
    #    platform overlay, then the plugins.
    # 3. Klipper's own dependencies.
    python3 -m venv venv && venv/bin/pip install greenlet cffi pyserial Jinja2 numpy

    # 4. FlashForge hardcodes GCC_CMD = "mips-linux-gnu-gcc" in
    #    klippy/chelper/__init__.py, so chelper will not build on a workstation
    #    without a shim of that name on PATH. Worth knowing for the printer too:
    #    it means a stock AD5X cannot rebuild chelper from source by itself.
    venv/bin/python klippy/klippy.py printer.cfg -l klippy.log

A good run reaches `klippy.py", line 131, in _connect` / `Unable to connect`,
having logged all 39 config sections and no `Config error`, `Unable to load
module` or unused-option complaint. Compare against the unpatched stock tree: the
two should get exactly as far as each other.

This does not exercise a print. It needs the MCU data dictionary and real
hardware, which is what the bench is for.

## Bringing it up on a printer

Our own boot integration, installer and recovery images are a later milestone.
Until they exist, the fastest way to put this layer in front of a real MCU is to
borrow the environment ZMOD already has on the rig: its chroot, its Moonraker,
its boot hooks. That is a deliberate shortcut for a proof of concept and not a
dependency of the design - the Klipper layer does not know or care what started
klippy.

**This replaces ZMOD's print path while it is installed.** ZMOD's own
`virtual_sdcard.py` is one of the files we overwrite, so ZMOD's features that
depend on it will not work until the files are put back. On a shared rig, take
the backup first and know how to undo it.

**ZMOD actively reverts an overlay dropped onto its tree.** `start.sh` runs
`fix_config.sh start`, which restores several klippy files to ZMOD's own copies
(for example it `cp`s `virtual_sdcard.py.orig` back whenever the live file
contains the string `zmod`). So copying our files in and then running `start.sh`
does not test our files - it tests ZMOD's. The way to run our overlay on a ZMOD
rig is to launch klippy directly against the deployed files, below, which is also
where an early failure's stderr is visible.

    # Find the tree and confirm what is actually in it before touching anything.
    ls -l "$KLIPPER_DIR"/klippy/extras/virtual_sdcard.py
    md5sum "$KLIPPER_DIR"/klippy/extras/*.py > /usr/data/klippy-before.md5

    # Back up every file we are about to replace, then copy ours in.
    # Restore is the same list in reverse, followed by a klipper restart.

### Restarting klippy on a ZMOD rig, verified

Two things about our test rig are worth writing down, because both wasted time.

**klippy is not managed by the screen app here.** `firmwareExe` is not running
and `gdb` is not installed, so ZMOD's `restart_klipper.sh` - which drives the QT
screen through GDB to trigger a restart - cannot work. klippy is launched at boot
by `/usr/prog/klipper/start.sh`, which is the thing to re-run.

**`klipperDaemon start` needs the environment `start.sh` sets.** klippy runs on
FlashForge's `/usr/prog/Python-3.8.2`, whose loader path is exported by
`start.sh` (`LD_LIBRARY_PATH` for Python-3.8.2, openssl-1.0.2d, libffi-3.4.4).
Call `klipperDaemon start` without it and klippy dies at once, silently:
`start-stop-daemon -b` sends its stderr to `/dev/null`, so the import failure
never reaches `printer.log` and the pidfile is left pointing at a dead pid. Run
`start.sh`, or export that `LD_LIBRARY_PATH` yourself, and it comes up.

When diagnosing a klippy that will not start, run it in the foreground with the
loader path set and read stderr directly - that is where an early failure lands,
never in the `-l` log:

    export LD_LIBRARY_PATH=/usr/prog/Python-3.8.2/lib:/usr/prog/openssl-1.0.2d/lib:/usr/prog/libffi-3.4.4/lib
    /usr/prog/Python-3.8.2/bin/python3 /usr/prog/klipper/klippy/klippy.py \
        /usr/data/config/printer.cfg -l /tmp/kt.log -a /tmp/uds-test

### Result, on the AD5X, 2026-08-25

The seven-file overlay was deployed over ZMOD 1.7.2 on stock 1.1.7, and klippy
started on the real hardware: both MCUs identified (`Loaded MCU 'mcu'` and
`Loaded MCU 'eboard'`, 116 commands each), both configured (`Configured MCU ...
(4096 moves)`), and the stats loop running - i.e. the Ready state. No config
error, no traceback, every patched extra loaded. The rig was then restored to
ZMOD byte-for-byte and its klippy brought back to `Printer is ready`.

`SHAPER_CALIBRATE` producing a graph through `.root/zshaper`, and a single-colour
print start to finish, are the remaining bench items. The four-colour path needs
the filament-system work and is not part of this layer.

### What is borrowed, and what replaces it

| Borrowed for the proof of concept | Replaced by | When |
|---|---|---|
| ZMOD's boot hooks and chroot mount | our own init chain and `/opt` bind mount | boot/install milestone |
| ZMOD's Moonraker and web stack | the Moonraker in our own userland | boot/install milestone |
| ZMOD's install and recovery path | our USB `.tgz`, boot-skip flag and uninstall image | boot/install milestone |
| ZMOD's reverse-engineered filament-system protocol | our own transport and state machine, Apache-2.0 attribution kept | filament milestone |

Nothing in the Klipper layer itself is borrowed. The seven AD5X files are derived
from FlashForge's stock and Forge-X's patches, and the derivation is reproducible
from `tools/klipper-merge/`.
