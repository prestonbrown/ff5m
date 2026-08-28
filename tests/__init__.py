## Host-side test package for Forge-X.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

"""Forge-X host-side tests and development-only tooling."""

## Make this directory importable by its own name, so shared test helpers can
## be imported the same way whichever runner is used. `unittest discover -s
## tests` puts this directory on sys.path; `discover -t .` and pytest import
## the package instead and do not. One line here beats a guarded import in
## every file that wants a helper.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
