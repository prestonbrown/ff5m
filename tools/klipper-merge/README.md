# Deriving the AD5X Klipper patches

Forge-X patches Klipper by shipping whole replacement files and symlinking them
over the stock tree (`.shell/S00init`, `apply_klipper_patches`). AD5X needs its own
copies because FlashForge's AD5X Klipper is not FlashForge's AD5M Klipper.

Rather than fork the patch set by hand, each AD5X file is a three-way merge:

    base   tools/klipper-merge/stock/ad5m   what Forge-X's patches were written against
    theirs .py/klipper/patches              Forge-X's AD5M patch set
    ours   tools/klipper-merge/stock/ad5x   AD5X stock, which we must not regress
    ->     .py/klipper/patches.ad5x         the AD5X override

Of the eleven patched files:

| Files | Handling |
|---|---|
| `gcode_move.py`, `led.py`, `temperature_sensor.py` | AD5M and AD5X stock are byte-identical. Forge-X's patch applies verbatim; **no override** |
| `gcode_shell_command.py` | Not in stock at all on either platform. Forge-X's file is the whole thing; **no override** |
| `configfile.py`, `gcode.py`, `buttons.py` | AD5X stock drifts, but nowhere Forge-X touched. Merged **mechanically**, byte-reproducible |
| `virtual_sdcard.py`, `resonance_tester.py`, `shaper_calibrate.py`, `statistics.py` | Real conflicts. **Hand-resolved**; see below |

## Running it

    tools/klipper-merge/merge.sh          # verify (the gate; runs in CI)
    tools/klipper-merge/merge.sh update   # regenerate after changing an input

`verify` checks three things, and each one has a way of going wrong that is silent
otherwise:

- A file listed as shared really does still have identical stock on both
  platforms. If FlashForge ever changes one of them on only one platform, the AD5M
  patch quietly stops being correct for AD5X.
- A mechanically merged file matches the merge exactly, so nobody has hand-edited
  a generated file where the edit would be lost on the next `update`.
- A hand-resolved file's inputs still hash to what they hashed to when a human
  resolved it. Any movement in Forge-X's patch or in FlashForge's stock means the
  resolution needs looking at again.

That last check hashes the **entire** merge output, not just the conflicted hunks,
because the worst defect found while doing this was not in a conflict.
`statistics.py` merged Forge-X's `disabled` option in and dropped the `if not
self.disabled:` that guards it, purely because AD5X had reordered the two lines
next to it. Klippy still started; the option just did nothing.

## The resolutions, and why

**`shaper_calibrate.py`** - AD5X ships a strictly newer shaper API than Forge-X was
written against: keyword arguments, plus `damping_ratio`, `shaper_freqs`,
`test_damping_ratios` and `max_freq` that Forge-X does not know about. Forge-X's own
contribution to this file was threading `scv` through, and **AD5X already does that
itself**, identically. So all four regions take AD5X and nothing of Forge-X's is
lost. Forge-X's two real fixes here - the `parent_conn.poll()` pipe-drain that
stops a large result deadlocking the child, and the `scv` in the smoothing warning
- are outside the conflicts and survive.

**`resonance_tester.py`** - the other half of the same interface, which is why the
two must be merged and tested together. Merged carelessly this raises `TypeError`
when a user runs input shaping, long after startup succeeded. The
`find_best_shaper` call takes AD5X's keyword form. The `save_calibration_data` call
and definition take **both** sides: `max_freq` reaches the CSV writer, and
`best_shaper`/`scv` reach Forge-X's JSON export - which is not optional, because
`.root/zshaper/calibrate_shaper.py` plots from that JSON and reads `scv`, `axis`,
`best_shaper`, `calibration_data.psd_*` and every field of `all_shapers`.

**`virtual_sdcard.py`** - on AD5X this file is also the tool-change path: stock
watches for `T0`-`T15` in the gcode stream and drives `load_channel`,
`print_channel`, `change_filament` and `enable_ffm` from it. That is the IFS hook,
and it has to survive intact. Both sides add independent state and independent
`get_status` keys, so those regions take both. `.gx` stays in `VALID_GCODE_EXTS`
even though Forge-X dropped it on AD5M, because it is FlashForge's own slicer
output and the stock UI writes it. The case-insensitive filename fallback takes
Forge-X's side: FlashForge commented it out on both platforms and Forge-X
deliberately turned it back on.

**`statistics.py`** - keeps Forge-X's `disabled` option and AD5X's 2-second stats
interval rather than Forge-X's 1-second one. This platform shuts the MCU down when
the host is busy, so the vendor's slower cadence is the one to keep.
