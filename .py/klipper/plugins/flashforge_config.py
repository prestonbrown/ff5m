## The printer's own settings file, /usr/prog/config/Adventurer5M.json.
##
## This is STOCK FlashForge state, not a mod artifact: it lives on the stock
## firmware partition and the stock UI reads and writes it. So it is the
## authority on what is in each slot, how many channels the machine has, and
## what the factory load/unload distances are - and anything we change here has
## to survive being read back by that UI.
##
## This module is the one place that knows FlashForge's file format. Everything
## above asks it questions and never opens the file itself.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import json
import logging
import os
import tempfile


PATH = "/usr/prog/config/Adventurer5M.json"

## FFMInfo indexes slots from 1. Index 0 is not a lane: it is the extruder's
## current material (`materialName`/`materialColor`). Treated as such here, and
## kept separate from the lanes so a wrong guess cannot silently become a fifth
## channel.
LOADED_SLOT = 0

## Stock's own filament-sensor thresholds. Recorded for reference only: our
## measured raw ADC is 0.008 present / 0.043 absent, nowhere near either. Stock is presumably
## comparing against a different scale (a resistance conversion), so these are
## exposed for reference and deliberately NOT wired into our classifier.
SENSOR_MIN_KEY = "FilamentSenserMin"
SENSOR_MAX_KEY = "FilamentSenserMax"


class FlashForgeConfig(object):
    """Read and write the printer's settings file without losing anything.

    Every write is read-modify-write of the whole document and lands through a
    temporary file and a rename, so a power cut cannot leave the stock UI with
    half a config.
    """

    def __init__(self, path=PATH):
        self.path = path

    ## -- raw document -------------------------------------------------------

    def load(self):
        with open(self.path, "r") as handle:
            return json.load(handle)

    def save(self, document):
        directory = os.path.dirname(self.path) or "."
        handle = tempfile.NamedTemporaryFile(
            mode="w", dir=directory, prefix=".ffconfig-", suffix=".tmp",
            delete=False, encoding="utf-8")
        try:
            with handle:
                json.dump(document, handle, indent=4, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except Exception:
            ## Never leave a stray temp file next to the printer's own config.
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    def update(self, mutate):
        """Apply `mutate(document)` and write the result back. Returns it.

        A settings file that does not exist yet is started rather than
        refused: an image without FlashForge's own UI has no reason to have
        one, and the slot registry still needs somewhere to live. Only a
        plain miss bootstraps - a file that exists but will not read is
        raised, because overwriting it could destroy something recoverable.
        """
        try:
            document = self.load()
        except FileNotFoundError:
            document = {}
        mutate(document)
        self.save(document)
        return document

    ## -- typed views --------------------------------------------------------

    def section(self, name, document=None):
        document = self.load() if document is None else document
        value = document.get(name)
        return value if isinstance(value, dict) else {}

    def channel_count(self, document=None):
        """How many lanes the file describes, counted from its own keys.

        NOT from `FFMInfo.channel`. That field reads 4 on a four-lane machine
        with lane 4 loaded, which is consistent with "the lane count" and with
        "the current lane" at the same time (current_channel below reads it as
        the latter). Deriving the count from it would list a single slot the
        day it means the current lane.
        Counting ffmType<n> keys needs no interpretation at all.

        `F13` has no channel_count on firmware 3.0.6 and `F19` reports it as an
        English word baked into the board's image, so this file is still the
        practical answer - just read a different way.
        """
        info = self.section("FFMInfo", document)
        slots = [int(key[len("ffmType"):]) for key in info
                 if key.startswith("ffmType") and key[len("ffmType"):].isdigit()]
        lanes = [slot for slot in slots if slot >= 1]
        return max(lanes) if lanes else None

    def current_channel(self, document=None):
        """Which lane FFMInfo says is loaded, or None."""
        value = self.section("FFMInfo", document).get("channel")
        if not isinstance(value, (int, float)):
            return None
        channel = int(value)
        return channel if channel >= 1 else None

    def is_enabled(self, document=None):
        return bool(self.section("FFMInfo", document).get("ffmEnable", False))

    def materials(self, document=None):
        """{slot: {"type", "color"}} for every lane, slot numbers from 1.

        A slot the printer has nothing for reads type "?" and colour "", which
        is normalised to None rather than passed through as a literal.
        """
        info = self.section("FFMInfo", document)
        count = self.channel_count(document) or 0
        return {slot: self._slot(info, slot) for slot in range(1, count + 1)}

    def loaded_material(self, document=None):
        """What is in the extruder, or None.

        Slot 0 is where a single-material AD5M records this, and it stays empty
        on a machine with an IFS - which is why this used to answer "none" on an
        AD5X no matter what was loaded. When FFMInfo names a lane, that lane is
        the answer.
        """
        info = self.section("FFMInfo", document)
        channel = self.current_channel(document)
        if channel is not None:
            return self._slot(info, channel)
        return self._slot(info, LOADED_SLOT)

    @staticmethod
    def _slot(info, slot):
        material = info.get("ffmType%d" % slot)
        colour = info.get("ffmColor%d" % slot)
        return {
            "type": None if material in (None, "", "?") else material,
            "color": None if colour in (None, "") else colour,
        }

    def set_material(self, slot, material=None, colour=None):
        """Write one slot back, leaving the rest of the document untouched.

        `None` clears a field to the printer's own empty markers, so a slot we
        empty looks to the stock UI exactly like one it emptied itself.
        """
        def mutate(document):
            info = document.setdefault("FFMInfo", {})
            info["ffmType%d" % slot] = "?" if material is None else material
            info["ffmColor%d" % slot] = "" if colour is None else colour

        self.update(mutate)

    ## -- factory motion parameters -----------------------------------------

    def multicolour(self, document=None):
        """Stock's own load, purge and unload distances and speeds.

        The `Multicolour` block, worth preferring over any constant we might
        invent: it is what the stock firmware itself runs with.
        """
        return dict(self.section("Multicolour", document))

    def stock_sensor_thresholds(self, document=None):
        """Stock's filament-sensor limits, for reference only.

        NOT used to classify: our measured raw ADC is an order of magnitude
        below these, so stock is comparing something else. See
        ifs_sensor_logic for the measured bands.
        """
        general = self.section("general", document)
        return (general.get(SENSOR_MIN_KEY), general.get(SENSOR_MAX_KEY))


def load_quietly(path=PATH):
    """Best-effort read; None when the file is missing or unreadable.

    A printer without this file is not an error worth refusing to start over -
    it just means we have no slot metadata.
    """
    try:
        return FlashForgeConfig(path).load()
    except (OSError, ValueError) as exc:
        logging.info("FlashForge config unavailable at %s: %s", path, exc)
        return None
