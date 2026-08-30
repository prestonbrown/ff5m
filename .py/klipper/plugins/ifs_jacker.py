## IFS Jacker support - a pass-through device on the IFS serial link.
##
##     [ifs_jacker]
##
## The IFS Jacker (https://github.com/ninjamida/ifs-jacker) sits between the
## host and the IFS board: F-opcode traffic passes through untouched, and the
## Jacker answers Z-opcodes of its own. This module detects one when present,
## tracks the peripheral state it appends to every status line, and exposes
## its Z commands - without any of it disturbing a machine with a plain IFS.
##
## Detection is the delicate part. A bare IFS board answers a Z command with
## silence, and silence is indistinguishable from a dead link - so the probe
## is capped (three attempts, then quiet until the link is re-established)
## and every silent probe is followed by an F13, which both proves the link
## alive and clears the board's input of the command it could not answer.
##
## Requires [ifs]; all exchanges go through its single reader.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import logging
import re
import time

PROBE_COMMAND = "Z2"
PROBE_CHASER = "F13"
PROBE_DELAY = 30.0
PROBE_RETRY_DELAY = 3.0
PROBE_ATTEMPTS = 3
STALE_AFTER = 5.0

## Firmware 3.0 added peripherals; below that only the identity query works.
PERIPHERALS_MIN_VERSION = 3.0

SOFTWARE_RE = re.compile(r'software:\s*"IFS Jacker')
VERSION_RE = re.compile(r'version:\s*"(\d+)\.(\d+)')
CHANNELS_RE = re.compile(r"channel_count:\s*(\d+)")
PERIPHERAL_COUNT_RE = re.compile(r"peripheral_count:\s*(\d+)")
PERIPHERAL_RE = re.compile(r"(?:\s|^)p(\d+)_(\w+?):\s*([^\s]+)")


class ProbeResult(object):
    """What a Z2 reply said, once parsed."""

    __slots__ = ("present", "version", "channel_count", "peripheral_count")

    def __init__(self, present, version, channel_count, peripheral_count):
        self.present = present
        self.version = version
        self.channel_count = channel_count
        self.peripheral_count = peripheral_count


def parse_probe(reply):
    """Read a Z2 reply. Absent detection (None) for anything but a Jacker."""
    if reply is None or not SOFTWARE_RE.search(reply):
        return None
    version = 0.0
    match = VERSION_RE.search(reply)
    if match is not None:
        version = float("%s.%s" % (match.group(1), match.group(2)))
    def count(pattern):
        found = pattern.search(reply)
        return int(found.group(1)) if found is not None else None
    return ProbeResult(True, version, count(CHANNELS_RE),
                       count(PERIPHERAL_COUNT_RE))


def parse_peripherals(raw):
    """{"id": {"param": value}} out of a status line the Jacker augmented.

    The Jacker appends `p<id>_<param>: <value>` tuples to the F13 payload;
    the board's own fields ignore them, and values arrive as int when they
    parse, float when they nearly do, strings otherwise.
    """
    peripherals = {}
    for match in PERIPHERAL_RE.finditer(raw or ""):
        peripheral, param, value = match.group(1), match.group(2), match.group(3)
        for cast in (int, float):
            try:
                value = cast(value)
                break
            except ValueError:
                continue
        peripherals.setdefault(peripheral, {})[param] = value
    return peripherals


class IfsJacker(object):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")

        self.probe_delay = config.getfloat("probe_delay", PROBE_DELAY, minval=0.)
        self.probe_attempts = config.getint("probe_attempts", PROBE_ATTEMPTS,
                                            minval=1, maxval=10)

        self.present = None       # None unknown, True/False detected
        self.version = 0.0
        self.channel_count = None
        self.peripheral_count = None
        self.peripherals = {}
        self._attempts = 0
        self._last_status_time = 0.0
        self._timer = None

        self.ifs = None
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        for name, handler in (
                ("IFSJ_CHECK", self.cmd_IFSJ_CHECK),
                ("IFSJ_Z1", self.cmd_IFSJ_Z1),
                ("IFSJ_Z2", self.cmd_IFSJ_Z2),
                ("IFSJ_Z3", self.cmd_IFSJ_Z3),
                ("IFSJ_Z4", self.cmd_IFSJ_Z4),
                ("IFSJ_Z5", self.cmd_IFSJ_Z5)):
            self.gcode.register_command(name, handler,
                                        desc="IFS Jacker %s" % name[5:])

    ## -- lifecycle -----------------------------------------------------------

    def _handle_ready(self):
        self.ifs = self.printer.lookup_object("ifs", None)
        if self.ifs is None:
            raise self.printer.config_error(
                "[ifs_jacker] requires [ifs]: declare it after the IFS itself")
        self.ifs.add_status_listener(self._on_status)
        self._timer = self.reactor.register_timer(
            self._tick, self.reactor.monotonic() + self.probe_delay)

    def _tick(self, eventtime):
        """Probe while undetected; watch for the link going away once found."""
        try:
            if self.present is None:
                self.probe()
                if self.present is None:
                    if self._attempts >= self.probe_attempts:
                        ## Quiet after the capped attempts: a plain IFS, which
                        ## is the normal case. Re-probe only after a reconnect.
                        self.present = False
                        logging.info("IFS Jacker: not present")
                    return self.reactor.monotonic() + PROBE_RETRY_DELAY
                return self.reactor.NEVER
            if (self.present and self._last_status_time
                    and eventtime - self._last_status_time > STALE_AFTER):
                ## The one reader has gone quiet; whatever it knew is stale.
                logging.info("IFS Jacker: IFS went away; state cleared")
                self._clear()
                return self.reactor.NEVER
            return eventtime + 1.0
        except Exception as exc:
            logging.warning("IFS Jacker: %s", exc)
            return eventtime + PROBE_RETRY_DELAY

    def _clear(self):
        self.present = None
        self.version = 0.0
        self.channel_count = None
        self.peripheral_count = None
        self.peripherals = {}
        self._attempts = 0
        self._timer = self.reactor.register_timer(
            self._tick, self.reactor.monotonic() + self.probe_delay)

    ## -- detection and state ------------------------------------------------

    def probe(self):
        """Ask once. Leaves `present` None on an inconclusive (silent) try."""
        self._attempts += 1
        try:
            reply = self.ifs.execute(PROBE_COMMAND)
        except Exception as exc:
            ## Silence, on this link, means exactly one thing: nothing on the
            ## far side answers Z opcodes. Chase with an F13 so the board's
            ## input queue does not hold the command it could not answer.
            logging.debug("IFS Jacker: probe unanswered (%s)", exc)
            try:
                self.ifs.execute(PROBE_CHASER)
            except Exception:
                pass
            return None
        result = parse_probe(reply.payload if hasattr(reply, "payload") else str(reply))
        if result is None:
            return None
        self.present = result.present
        self.version = result.version
        self.channel_count = result.channel_count
        self.peripheral_count = result.peripheral_count or 0
        self.peripherals = {str(i): {} for i in range(self.peripheral_count)}
        logging.info("IFS Jacker: present (version %.1f, %s channels, "
                     "%d peripherals)", self.version,
                     self.channel_count or "?", self.peripheral_count)
        return result

    def _on_status(self, status):
        self._last_status_time = time.monotonic()
        if not self.present or self.version < PERIPHERALS_MIN_VERSION:
            return
        self.peripherals = parse_peripherals(getattr(status, "raw", ""))

    def get_status(self, eventtime=None):
        return {
            "detected": self.present is True,
            "known": self.present is not None,
            "version": self.version if self.present else 0.0,
            "channel_count": self.channel_count if self.present else None,
            "peripheral_count": (self.peripheral_count or 0) if self.present else 0,
            "peripherals": dict(self.peripherals) if self.present else {},
        }

    ## -- gcode ---------------------------------------------------------------

    def _require(self, minimum_version):
        if self.present is not True:
            self.gcode.respond_info("IFS Jacker: not present")
            return False
        if self.version < minimum_version:
            self.gcode.respond_info(
                "IFS Jacker: firmware %.1f required, this one is %.1f"
                % (minimum_version, self.version))
            return False
        return True

    def cmd_IFSJ_CHECK(self, gcmd):
        result = self.probe()
        if result is None:
            gcmd.respond_info("IFS Jacker: no answer to the probe")
            return
        gcmd.respond_info("IFS Jacker: version %.1f, %s channels, %d peripherals"
                          % (self.version, self.channel_count or "?",
                             self.peripheral_count or 0))

    def _send(self, command):
        reply = self.ifs.execute(command)
        return getattr(reply, "payload", None) or str(reply)

    def cmd_IFSJ_Z1(self, gcmd):
        if self._require(0.0):
            gcmd.respond_info("Z1 > %s" % self._send("Z1"))

    def cmd_IFSJ_Z2(self, gcmd):
        if self._require(0.0):
            gcmd.respond_info("Z2 > %s" % self._send("Z2"))

    def cmd_IFSJ_Z3(self, gcmd):
        if self._require(3.0):
            gcmd.respond_info("Z3 > %s" % self._send("Z3"))

    def cmd_IFSJ_Z4(self, gcmd):
        if self._require(3.0):
            gcmd.respond_info("Z4 > %s" % self._send("Z4"))

    def cmd_IFSJ_Z5(self, gcmd):
        if not self._require(2.2):
            return
        peripheral = gcmd.get_int("PERIPHERAL", 0, minval=0)
        command = gcmd.get_int("COMMAND", 2, minval=0)
        param1 = gcmd.get_int("PARAM1", 0)
        param2 = gcmd.get_int("PARAM2", 0)
        gcmd.respond_info("Z5 > %s" % self._send(
            "Z5 C%d F%d L%d S%d" % (peripheral, command, param1, param2)))


def load_config(config):
    return IfsJacker(config)
