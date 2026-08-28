## Shared klipper plumbing for the AD5X filament sensors.
##
## Both sensors present as stock `filament_switch_sensor` objects - that is the
## surface Moonraker and HelixScreen already subscribe to, so nothing above has
## to learn a new name. What differs between them is only where the reading
## comes from, which is the one method a subclass implements.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import inspect
import logging

from . import filament_switch_sensor


DEFAULT_CHECK_INTERVAL = 0.5


class IfsSensorBase(object):
    """A filament sensor whose reading comes from somewhere non-standard.

    Subclasses implement `read_present()` returning True, False, or None for
    "no reading yet". None is not a runout: it is left at the last known state
    rather than reported as absent, because a sensor that has not answered must
    never pause a print by itself.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.check_interval = config.getfloat(
            "check_interval", DEFAULT_CHECK_INTERVAL, above=0.05)

        self.runout_helper = filament_switch_sensor.RunoutHelper(config)
        self.get_status = self.runout_helper.get_status

        ## note_filament_present gained an `eventtime` first argument in newer
        ## klipper. This tree still has the one-argument form; ask rather than
        ## assume, so the shim survives a klipper bump either way.
        self._wants_eventtime = "eventtime" in inspect.signature(
            self.runout_helper.note_filament_present).parameters

        ## Registering under klipper's own name is what makes this visible to
        ## Moonraker and to anything already watching filament sensors.
        self.printer.add_object("filament_switch_sensor %s" % self.name, self)

        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self._timer = None

    ## -- subclass hook ------------------------------------------------------

    def read_present(self):
        raise NotImplementedError

    ## -- plumbing -----------------------------------------------------------

    def _handle_ready(self):
        self._timer = self.reactor.register_timer(self._check, self.reactor.NOW)

    def _note(self, present):
        if self._wants_eventtime:
            self.runout_helper.note_filament_present(
                self.reactor.monotonic(), present)
        else:
            self.runout_helper.note_filament_present(present)

    def _check(self, eventtime):
        try:
            present = self.read_present()
        except Exception as exc:
            ## A throwing sensor must not take klipper down, and must not be
            ## read as a runout either.
            logging.warning("IFS sensor %s: read failed: %s", self.name, exc)
            present = None
        if present is not None:
            self._note(bool(present))
        return eventtime + self.check_interval
