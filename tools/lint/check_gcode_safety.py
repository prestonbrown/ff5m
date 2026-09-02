#!/usr/bin/env python3
"""Every gcode command must fail the COMMAND, never the printer.

Klipper turns any exception that is not a gcode error into "Internal error on
command", which puts klippy into SHUTDOWN and takes the MCUs down with it. That
is a wildly disproportionate outcome for most of what a command does, and it
has happened twice on the AD5X:

  - a refused IFS opcode raised RuntimeError out of a feed, shutting the printer
    down mid-load and needing a FIRMWARE_RESTART
  - TONE hit a PWM device that does not exist on this board, and a filament
    change that had completely succeeded died on the chirp afterwards

So a registered handler has to either contain a `try`, or say why it does not
need one with a `GCODE_SAFE:` comment. Neither proves correctness - the point is
that the author had to look at the question once.

Handlers in upstream files we have not touched are listed in
gcode_safety_baseline.txt. That list may shrink and must never grow.

Usage: check_gcode_safety.py [ROOT]
"""

import ast
import pathlib
import sys


ANNOTATION = "GCODE_SAFE:"
REGISTER = ("register_command", "register_mux_command")
BASELINE = pathlib.Path(__file__).with_name("gcode_safety_baseline.txt")


def baseline():
    """Known-unguarded handlers, as `path:handler` lines. Blanks and # ignored."""
    if not BASELINE.exists():
        return set()
    out = set()
    for line in BASELINE.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def handler_names(tree):
    """Handler attribute names passed to register_command, in source order."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in REGISTER:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Attribute) and arg.attr.startswith("cmd_"):
                found.append((arg.attr, node.lineno))
    return found


def functions(tree):
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def guarded(func, lines):
    if any(isinstance(n, ast.Try) for n in ast.walk(func)):
        return True
    ## The annotation may sit just above the def or anywhere inside it.
    start = max(func.lineno - 4, 1)
    end = max(getattr(func, "end_lineno", func.lineno), func.lineno)
    return any(ANNOTATION in line for line in lines[start - 1:end])


def check(root):
    problems = []
    checked = 0
    for path in sorted(pathlib.Path(root).rglob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            problems.append((path, 0, "?", "does not parse: %s" % exc))
            continue
        lines = source.splitlines()
        defined = functions(tree)
        for name, lineno in handler_names(tree):
            func = defined.get(name)
            if func is None:
                continue          # registered elsewhere; not ours to judge
            checked += 1
            if not guarded(func, lines):
                problems.append((path, func.lineno, name,
                                 "no try and no %s comment" % ANNOTATION))
    return checked, problems


def main(argv):
    root = argv[1] if len(argv) > 1 else ".py/klipper/plugins"
    checked, found = check(root)
    known = baseline()
    seen = set()
    problems = []
    for path, lineno, name, why in found:
        key = "%s:%s" % (path, name)
        seen.add(key)
        if key in known:
            continue
        problems.append((path, lineno, name, why))
    for path, lineno, name, why in problems:
        print("%s:%d: %s: %s" % (path, lineno, name, why))

    ## The ratchet: a baselined handler that got fixed must leave the list, or
    ## the list stops meaning anything.
    stale = sorted(known - seen)
    for key in stale:
        print("%s: guarded now - remove it from %s" % (key, BASELINE.name))

    print("\n%d gcode command handlers checked, %d unguarded (%d baselined)"
          % (checked, len(problems), len(known & seen)))
    if stale and not problems:
        return 1
    if problems:
        print("\nA handler must convert failures into gcmd.error, or say why it\n"
              "cannot fail, because klipper turns anything else into a printer\n"
              "shutdown. Add a try, or a `## %s <reason>` comment." % ANNOTATION)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
