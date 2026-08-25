"""Stub Klipper objects so klippy plugins can be unit tested off-printer.

A Klipper extra receives a config object, pulls the printer from it, looks up
the gcode object, and registers commands. None of that needs hardware, so a
small stub is enough to exercise a plugin's real logic.
"""

import json
import sys
from pathlib import Path

import pytest

PLUGINS_DIR = Path(__file__).resolve().parents[2] / ".py" / "klipper" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))


class StubGCode:
    def __init__(self):
        self.commands = {}
        self.responses = []
        self.scripts = []

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def respond_info(self, msg):
        self.responses.append(msg)

    def run_script_from_command(self, script):
        self.scripts.append(script)

    def error(self, msg):
        return RuntimeError(msg)


class StubReactor:
    def monotonic(self):
        return 0.0


class StubPrinter:
    # mod_params raises via self.printer.command_error(...) on its failure paths.
    # Without this, the first bad-declaration test dies on AttributeError instead
    # of exercising the branch it targets.
    command_error = RuntimeError

    def __init__(self):
        self.objects = {"gcode": StubGCode()}
        self.event_handlers = {}

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def load_object(self, config, name):
        return self.objects.setdefault(name, StubGCodeMacro())

    def get_reactor(self):
        return StubReactor()

    def register_event_handler(self, event, handler):
        self.event_handlers.setdefault(event, []).append(handler)


class StubGCodeMacro:
    def load_template(self, config, name):
        return None


class StubConfig:
    """Mimics Klipper's ConfigWrapper for the subset plugins actually use."""

    def __init__(self, values, printer=None):
        self._values = values
        self._printer = printer or StubPrinter()

    def get_printer(self):
        return self._printer

    def get(self, key, default=object()):
        if key in self._values:
            return self._values[key]
        if isinstance(default, object) and default.__class__ is object:
            raise KeyError("missing required config option: %s" % key)
        return default

    def getint(self, key, default=None, minval=None, maxval=None):
        return int(self._values.get(key, default))

    def getboolean(self, key, default=None):
        return bool(self._values.get(key, default))

    def error(self, msg):
        return RuntimeError(msg)


@pytest.fixture
def declaration_file(tmp_path):
    """A minimal mod_params declaration covering a scalar and an enum."""
    decl = {
        "enums": {
            "DisplayEnum": {
                "type": "int",
                "values": {"STOCK": 0, "GUPPY": 3},
            }
        },
        "parameters": [
            {
                "key": "backlight",
                "type": "int",
                "default": 50,
                "label": "Backlight",
            },
            {
                "key": "display",
                "type": "DisplayEnum",
                "default": "STOCK",
                "label": "Display",
                "options": {"STOCK": "Stock screen", "GUPPY": "Guppy screen"},
            },
        ],
    }
    path = tmp_path / "mod_params.json"
    path.write_text(json.dumps(decl))
    return path


class StubGCommand:
    """Mimics Klipper's GCodeCommand for the four methods mod_params uses."""

    def __init__(self, **params):
        self._params = params
        self.raw = []
        self.info = []

    def get(self, key, default=None):
        if key in self._params:
            return self._params[key]
        if default is None and key in ("PARAM", "VALUE"):
            raise RuntimeError("missing required gcode parameter: %s" % key)
        return default

    def respond_info(self, msg):
        self.info.append(msg)

    def respond_raw(self, msg):
        self.raw.append(msg)

    def error(self, msg):
        return RuntimeError(msg)


@pytest.fixture
def variables_path(tmp_path):
    """The INI file mod_params persists to. Starts empty, as on a fresh install."""
    path = tmp_path / "variables.cfg"
    path.write_text("")
    return path


@pytest.fixture
def stub_config(declaration_file, variables_path):
    return StubConfig(
        {
            "declaration": str(declaration_file),
            "filename": str(variables_path),
        }
    )


@pytest.fixture
def readonly_config(tmp_path, variables_path):
    """A declaration containing a readonly parameter, for the refusal test."""
    decl = {
        "enums": {},
        "parameters": [
            {
                "key": "ro",
                "type": "int",
                "default": 1,
                "label": "Readonly",
                "readonly": True,
            }
        ],
    }
    path = tmp_path / "readonly_params.json"
    path.write_text(json.dumps(decl))
    return StubConfig(
        {"declaration": str(path), "filename": str(variables_path)}
    )


@pytest.fixture
def gcmd():
    """Factory for stub gcode commands: gcmd(PARAM="x", VALUE="1")."""
    return StubGCommand
