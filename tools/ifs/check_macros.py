#!/usr/bin/env python3
"""Compile every gcode: template in a klipper config the way klipper does.

Catches Jinja syntax errors without deploying anything or restarting klippy.
"""
import re, sys
import jinja2

path = sys.argv[1]
text = open(path).read()

## Sections and their gcode: blocks, indentation-delimited like klipper's parser.
sections = re.findall(r"^\[(gcode_macro [^\]]+)\]\n(.*?)(?=^\[|\Z)",
                      text, re.M | re.S)
env = jinja2.Environment(undefined=jinja2.StrictUndefined)
checked = failed = 0
for name, body in sections:
    m = re.search(r"^gcode:\n(.*?)(?=^\w|\Z)", body, re.M | re.S)
    if not m:
        print("  %-34s no gcode: block" % name)
        continue
    template = "\n".join(line[4:] if line.startswith("    ") else line
                         for line in m.group(1).splitlines())
    checked += 1
    try:
        env.parse(template)
        print("  %-34s OK" % name)
    except jinja2.TemplateSyntaxError as exc:
        failed += 1
        print("  %-34s LINE %s: %s" % (name, exc.lineno, exc.message))
print("\n%d templates, %d failed" % (checked, failed))
sys.exit(1 if failed else 0)
