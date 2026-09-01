## Publishes whether the running job begins with a tool change.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license
##
## A job that opens with a tool change makes part of the start flow
## redundant: the swap begins by cutting the loaded filament, so a nozzle
## flush ahead of it is filament pushed out only to be severed with the
## rest. The slicer knows this and could say so, but no re-slicing should
## be needed for a start flow to be sensible, so the open job's own head
## is read instead: the first tool command before the first extrusion is
## the job's opening tool, and anything after that is not "at start".
##
## The scan runs on demand from a macro template (printer.start_hint),
## covers the first 64 KiB of the file, and is cached per
## path/size/mtime so a template that reads it several times during one
## start scans once. Reading a file the virtual_sdcard already holds open
## is safe: the data is on disk before klippy ever names the file.

import os
import re

TOOL_LINE = re.compile(r"^T(\d+)\b")
EXTRUSION_WORD = re.compile(r"(?:^|\s)E-?\d")
SCAN_BYTES = 65536


class StartHint:
    def __init__(self, config):
        self.printer = config.get_printer()
        self._cache_key = None
        self._first_tool = None

    def get_status(self, eventtime=None):
        try:
            path = self.printer.lookup_object("virtual_sdcard").file_path or ""
        except Exception:
            path = ""
        if not path:
            self._cache_key = None
            self._first_tool = None
            return {"first_tool": None}
        try:
            stat = os.stat(path)
            key = (path, stat.st_size, stat.st_mtime_ns)
        except OSError:
            return {"first_tool": self._first_tool}
        if key != self._cache_key:
            self._cache_key = key
            self._first_tool = self._scan(path)
        return {"first_tool": self._first_tool}

    def _scan(self, path):
        try:
            with open(path, "r", errors="replace") as handle:
                head = handle.read(SCAN_BYTES)
        except OSError:
            return None
        for raw_line in head.splitlines():
            code = raw_line.split(";", 1)[0].strip()
            if not code:
                continue
            tool = TOOL_LINE.match(code)
            if tool:
                return int(tool.group(1))
            if EXTRUSION_WORD.search(code):
                # The job is extruding on whatever is loaded; a tool
                # command from here on is a mid-print change, not an
                # opening one.
                return None
        return None


def load_config(config):
    return StartHint(config)
