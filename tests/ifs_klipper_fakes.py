## Just enough klipper to construct and drive the IFS extras.
##
## Only what these objects actually touch - a fake that mirrors klipper's real
## surface would be a second implementation to maintain, and would drift.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import types

import ifs_modules


class FakeReactor:
    NOW = 0.0
    NEVER = float("inf")

    def __init__(self):
        self.timers = []
        self.async_callbacks = []
        self._now = 100.0

    def monotonic(self):
        return self._now

    def register_timer(self, callback, when=None):
        self.timers.append(callback)
        return callback

    def register_async_callback(self, callback, waketime=None):
        self.async_callbacks.append(callback)

    def run_async(self):
        """Drain what the poll thread handed back, as the reactor would."""
        pending, self.async_callbacks = self.async_callbacks, []
        for callback in pending:
            callback(self._now)


class FakeGCode:
    def __init__(self):
        self.commands = {}
        self.mux = {}
        self.responses = []

    def register_command(self, name, handler, desc=None):
        self.commands[name] = handler

    def register_mux_command(self, name, key, value, handler, desc=None):
        self.mux[(name, value)] = handler

    def respond_info(self, message):
        self.responses.append(message)

    ## A gcmd is only ever asked to respond in this code.
    def command(self):
        return self


class FakeGcmd:
    """klipper's parsed-command object, as far as these extras use it."""

    class error(Exception):
        pass

    def __init__(self, params=None, gcode=None):
        self.params = dict(params or {})
        self.gcode = gcode or FakeGCode()
        self.responses = self.gcode.responses

    def get(self, key, default=Ellipsis):
        if key in self.params:
            return str(self.params[key])
        if default is Ellipsis:
            raise self.error("missing %s" % key)
        return default

    def get_int(self, key, default=Ellipsis, minval=None, maxval=None):
        if key not in self.params:
            if default is Ellipsis:
                raise self.error("missing %s" % key)
            return default
        value = int(self.params[key])
        if minval is not None and value < minval:
            raise self.error("%s below %s" % (key, minval))
        if maxval is not None and value > maxval:
            raise self.error("%s above %s" % (key, maxval))
        return value

    def respond_info(self, message):
        self.gcode.respond_info(message)


class FakePrinter:
    def __init__(self):
        self.reactor = FakeReactor()
        self.objects = {"gcode": FakeGCode()}
        self.events = {}
        self.sent = []

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=Ellipsis):
        if name in self.objects:
            return self.objects[name]
        if default is Ellipsis:
            raise KeyError(name)
        return default

    def add_object(self, name, obj):
        self.objects[name] = obj

    def load_object(self, config, name):
        return self.objects.setdefault(name, types.SimpleNamespace())

    def register_event_handler(self, event, handler):
        self.events.setdefault(event, []).append(handler)

    def send_event(self, event, *args):
        self.sent.append((event, args))

    def fire(self, event):
        for handler in self.events.get(event, []):
            handler()


class FakeConfig:
    def __init__(self, name, values=None, printer=None):
        self._name = name
        self._values = dict(values or {})
        self.printer = printer or FakePrinter()

    def get_name(self):
        return self._name

    def get_printer(self):
        return self.printer

    def get(self, key, default=Ellipsis):
        if key in self._values:
            return self._values[key]
        if default is Ellipsis:
            raise KeyError(key)
        return default

    def _typed(self, key, default, cast):
        if key in self._values:
            return cast(self._values[key])
        if default is Ellipsis:
            raise KeyError(key)
        return default

    def getfloat(self, key, default=Ellipsis, **kwargs):
        return self._typed(key, default, float)

    def getint(self, key, default=Ellipsis, **kwargs):
        return self._typed(key, default, int)

    def getboolean(self, key, default=Ellipsis, **kwargs):
        return self._typed(key, default, bool)


class FakeRunoutHelper:
    """Stands in for klipper's RunoutHelper, recording what it was told."""

    def __init__(self, config):
        self.config = config
        self.notes = []
        self.filament_present = None

    def note_filament_present(self, is_filament_present):
        self.filament_present = is_filament_present
        self.notes.append(is_filament_present)

    def get_status(self, eventtime=None):
        return {"filament_detected": bool(self.filament_present)}


def install_filament_switch_sensor():
    """Provide the klipper module the sensor shims import."""
    module = types.ModuleType("filament_switch_sensor")
    module.RunoutHelper = FakeRunoutHelper
    return ifs_modules.provide("filament_switch_sensor", module)
