## Load the IFS klipper extras the way klipper does: as a package.
##
## These modules use relative imports, which is what klipper's extras are and
## what makes them self-contained - a bare sibling import fails there, and only
## works for Forge-X's feather modules because feather_screen.py mutates
## sys.path on the way past. So the tests build a real package rather than
## loading files in isolation.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import importlib.util
import pathlib
import sys
import types


PLUGIN_DIR = (pathlib.Path(__file__).parents[1] / ".py" / "klipper" / "plugins")
PACKAGE = "ifs_under_test"


def package():
    if PACKAGE not in sys.modules:
        pkg = types.ModuleType(PACKAGE)
        pkg.__path__ = [str(PLUGIN_DIR)]
        sys.modules[PACKAGE] = pkg
    return sys.modules[PACKAGE]


def provide(name, module):
    """Stand in for a klipper module the shims import, e.g. RunoutHelper's."""
    setattr(package(), name, module)
    sys.modules["%s.%s" % (PACKAGE, name)] = module
    return module


def load(name):
    """Import one plugin module inside the package, cached."""
    qualified = "%s.%s" % (PACKAGE, name)
    if qualified in sys.modules:
        return sys.modules[qualified]
    spec = importlib.util.spec_from_file_location(
        qualified, PLUGIN_DIR / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    setattr(package(), name, module)
    spec.loader.exec_module(module)
    return module
