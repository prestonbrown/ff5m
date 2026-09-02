## What is in each IFS slot.
##
##     [ifs_materials]
##
## Slot contents are the printer's own state, kept in FlashForge's
## Adventurer5M.json and shared with the stock UI, so this object is a view onto
## that file rather than a second copy of the truth. Write through
## IFS_SET_MATERIAL and the stock UI sees the change too.
##
## Deliberately separate from [ifs]: the board knows whether a lane has filament
## in it, and this knows what that filament is. Neither needs the other, and the
## file is readable on a machine whose IFS is unplugged.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import logging
import os
import re

from . import flashforge_config


## #RGB or #RRGGBB, which is what the stock UI writes and what is stored. The
## gcode surface must also take the same digits bare: klipper's parser treats
## '#' as a comment start, so COLOR=#FF8800 arrives with the value empty and
## COLOR="#FF8800" is rejected as a malformed command before it reaches us.
## Stored form is always '#'-prefixed whatever the caller spelled.
COLOUR = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

## What each material wants the nozzle at to be pushed through it.
##
## These are handling temperatures, not print temperatures. They only have to
## melt the filament enough to move; the slicer still owns what a print runs at.
TEMPERATURES = {
    "PLA": 220.0,
    "PLA-CF": 220.0,
    "SILK": 230.0,
    "TPU": 230.0,
    "ABS": 250.0,
    "PETG": 250.0,
    "PETG-CF": 250.0,
}
## Prefix for adding or overriding one:
##
##     [ifs_materials]
##     temp_ASA: 260
##     temp_PLA: 215
TEMPERATURE_PREFIX = "temp_"

## The shortest useful first purge pass, mm. The hotend alone holds ~35 mm of
## filament (the refill distance after a cut), so a shorter pass never pushes
## new colour out of the nozzle at all - it would finish with the print
## starting on stale filament. 50 fills the hotend AND flushes it; the full
## setting above that is what an unknown or maximally distant colour gets.
PURGE_FLOOR_MM = 50.0


def _rgb(value):
    """(r, g, b) 0-255 from '#RGB'/'#RRGGBB', or None if not a colour."""
    if not value:
        return None
    digits = value.lstrip("#")
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    try:
        raw = bytes.fromhex(digits)
    except ValueError:
        return None
    return raw if len(raw) == 3 else None


def colour_distance(outgoing, incoming):
    """How far apart two colours are, 0.0 (identical) to 1.0 (black/white).

    Plain Euclidean RGB, normalised so opposite corners of the cube are 1.0.
    None when either colour is unknown - the caller cannot scale what it does
    not know, and must fall back to the full-length purge.
    """
    pair = [_rgb(value) for value in (outgoing, incoming)]
    if None in pair:
        return None
    total = sum((a - b) ** 2 for a, b in zip(*pair))
    return (total / (3.0 * 255 * 255)) ** 0.5


def _at(temp):
    """Suffix for a report line: the handling temperature, or nothing."""
    return "" if temp is None else " @ %.0fC" % temp


class IfsMaterials(object):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.path = config.get("path", flashforge_config.PATH)
        self.config_file = flashforge_config.FlashForgeConfig(self.path)

        self.temperatures = dict(TEMPERATURES)
        for option in config.get_prefix_options(TEMPERATURE_PREFIX):
            name = option[len(TEMPERATURE_PREFIX):].upper()
            ## above=0 rather than a hot-end range: this is a lookup table, and
            ## the macros refuse to move filament below 150 anyway. A user with
            ## a hotter hot end should not have to argue with us about it.
            self.temperatures[name] = config.getfloat(option, above=0.)

        self._cache = None
        self._cache_stamp = None

        gcode = self.printer.lookup_object("gcode")
        gcode.register_command("IFS_MATERIALS", self.cmd_IFS_MATERIALS,
                               desc="Report what each IFS slot is loaded with")
        gcode.register_command("IFS_SET_MATERIAL", self.cmd_IFS_SET_MATERIAL,
                               desc="Set an IFS slot's material type and colour")

    ## -- reading ------------------------------------------------------------

    def _document(self):
        """The file, re-read only when it changes underneath us.

        The stock UI writes this file too, so caching on mtime keeps Moonraker's
        polling cheap without going stale after someone uses the panel.
        """
        try:
            stamp = os.stat(self.path).st_mtime_ns
        except OSError as exc:
            logging.info("ifs_materials: %s unavailable: %s", self.path, exc)
            self._cache, self._cache_stamp = None, None
            return None
        if stamp != self._cache_stamp:
            try:
                self._cache = self.config_file.load()
                self._cache_stamp = stamp
            except (OSError, ValueError) as exc:
                logging.warning("ifs_materials: cannot read %s: %s",
                                self.path, exc)
                self._cache, self._cache_stamp = None, None
        return self._cache

    def slots(self):
        """{slot: {"type", "color"}}, empty when the file is unreadable."""
        document = self._document()
        return {} if document is None else self.config_file.materials(document)

    def material(self, slot):
        return self.slots().get(slot)

    def temperature(self, material):
        """What to heat the nozzle to in order to move this material.

        None for a material we have no number for, INCLUDING an empty slot.
        Substituting PLA's 220 there quietly runs ABS at 220 and snaps it off
        in the heatbreak; saying "I do not know" lets the caller
        insist on a TEMP= instead of guessing on the user's behalf.
        """
        if not material:
            return None
        return self.temperatures.get(material.upper())

    def purge_first_mm(self, slots):
        """{"from>to": first-pass purge mm} for every pair of KNOWN colours.

        Scaled between PURGE_FLOOR_MM and the printer's own first_purge_mm by
        colour distance, so close colours flush less than opposite ones. A
        pair missing from the table - an unknown colour on either side, or no
        [ifs] object to ask for the full length - makes the caller purge the
        full setting, which is the safe answer.
        """
        ifs = self.printer.lookup_object("ifs", None)
        if ifs is None:
            return {}
        full = float(ifs.params.first_purge_mm)
        floor = min(PURGE_FLOOR_MM, full)
        table = {}
        for from_slot, from_value in slots.items():
            for to_slot, to_value in slots.items():
                if from_slot == to_slot:
                    continue
                distance = colour_distance(from_value.get("color"),
                                           to_value.get("color"))
                if distance is None:
                    continue
                table["%s>%s" % (from_slot, to_slot)] = (
                    floor + distance * (full - floor))
        return table

    def _recorded_lane(self):
        """The lane WE know is loaded, if anything is keeping that record.

        FlashForge's own `FFMInfo.channel` is a fine answer on a stock machine,
        where the stock UI maintains it. Under Forge-X nothing writes it, so it
        is whatever it was the last time stock ran - it agrees with reality until
        the first tool change and then quietly does not. save_variables is the
        live record, so prefer it and fall back to the file.
        """
        variables = self.printer.lookup_object("save_variables", None)
        if variables is None:
            return None
        slot = getattr(variables, "allVariables", {}).get("ifs_loaded")
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return None
        return slot if slot >= 1 else None

    def get_status(self, eventtime=None):
        document = self._document()
        if document is None:
            return {"available": False, "channel_count": None,
                    "enabled": False, "slots": {}, "loaded": None,
                    "purge_first_mm": {},
                    "temperatures": dict(self.temperatures)}
        slots = self.config_file.materials(document)
        recorded = self._recorded_lane()
        loaded = (slots.get(recorded) if recorded is not None
                  else self.config_file.loaded_material(document))
        return {
            "available": True,
            "channel_count": self.config_file.channel_count(document),
            "enabled": self.config_file.is_enabled(document),
            ## Moonraker serialises dict keys as strings; be explicit about it
            ## rather than leaving consumers to discover it.
            ## Each slot carries its handling temperature so a macro can ask one
            ## question instead of doing the lookup in Jinja.
            "slots": {str(slot): dict(value,
                                      temp=self.temperature(value.get("type")))
                      for slot, value in slots.items()},
            ## The colour-distance purge table, "from>to" keyed, for the load
            ## macros. Absent pairs mean "full length" to the caller.
            "purge_first_mm": self.purge_first_mm(slots),
            "loaded": None if loaded is None else dict(
                loaded, temp=self.temperature(loaded.get("type"))),
            "temperatures": dict(self.temperatures),
        }

    ## -- gcode --------------------------------------------------------------

    ## GCODE_SAFE: the file read is inside _document(), which turns a
    ## missing or unparseable Adventurer5M.json into available=False. A
    ## printer whose stock config is gone still answers this command.
    def cmd_IFS_MATERIALS(self, gcmd):
        info = self.get_status()
        if not info["available"]:
            gcmd.respond_info("IFS materials: %s is unreadable" % self.path)
            return
        loaded = info["loaded"] or {}
        gcmd.respond_info("IFS materials (%s channels, %s)"
                          % (info["channel_count"],
                             "enabled" if info["enabled"] else "disabled"))
        for slot in sorted(info["slots"], key=int):
            entry = info["slots"][slot]
            gcmd.respond_info("  slot %s: %s %s%s"
                              % (slot, entry["type"] or "empty",
                                 entry["color"] or "",
                                 _at(entry["temp"])))
        gcmd.respond_info("  loaded: %s %s%s"
                          % (loaded.get("type") or "none",
                             loaded.get("color") or "",
                             _at(loaded.get("temp"))))

    def cmd_IFS_SET_MATERIAL(self, gcmd):
        slot = gcmd.get_int("SLOT", minval=0)
        document = self._document()
        ## None when the settings file does not exist yet: there is nothing
        ## to count lanes from, and the range check simply has no answer.
        count = (self.config_file.channel_count(document)
                 if document is not None else None)
        if count is not None and slot > count:
            raise gcmd.error("SLOT %d is out of range; this printer has %d"
                             % (slot, count))
        ## Absent means "leave alone"; empty means "clear it".
        material = gcmd.get("TYPE", None)
        colour = gcmd.get("COLOR", None)
        if material is None and colour is None:
            raise gcmd.error("IFS_SET_MATERIAL needs TYPE= or COLOR=")
        if colour not in (None, "") and not COLOUR.match(colour):
            raise gcmd.error("COLOR must be RGB or RRGGBB hex ('#' optional;"
                             " the parser drops it), got %r" % colour)
        if colour not in (None, "") and not colour.startswith("#"):
            colour = "#" + colour

        current = self.material(slot) or {"type": None, "color": None}
        try:
            self.config_file.set_material(
                slot,
                current["type"] if material is None else (material or None),
                current["color"] if colour is None else (colour or None))
        except (OSError, ValueError) as exc:
            ## OSError: the write itself failed. ValueError: a settings file
            ## that exists but does not parse - never overwritten, so the
            ## operator gets to recover what is in it. Both as gcode errors;
            ## a raw exception out of a command handler is a shutdown on a
            ## console-driven host.
            raise gcmd.error("cannot write %s: %s" % (self.path, exc))
        self._cache_stamp = None      # force a re-read on the next question
        updated = self.material(slot) or {}
        gcmd.respond_info("slot %d: %s %s" % (slot, updated.get("type") or "empty",
                                              updated.get("color") or ""))


def load_config(config):
    return IfsMaterials(config)
