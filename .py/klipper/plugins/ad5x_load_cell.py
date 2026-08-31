##
## Zero the AD5X's load cell before anything probes with it.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license
##
## The AD5X probes with a load cell whose conditioning MCU hangs off a plain
## UART, and klipper only ever sees that MCU's verdict as a digital pin. The
## MCU decides "triggered" by comparing the cell against a stored zero, and
## that zero drifts: measured at 140 g of standing offset on an idle machine
## with the nozzle hanging in free air, which is enough to hold the pin
## triggered permanently. Every probing move then fails before it starts with
## "Probe triggered prior to movement", and a bed mesh can never complete.
##
## Stock re-zeroes from its own screen application before each leveling flow.
## Nothing in klipper did, which is why this exists: the AD5M has a tare
## plugin and the AD5X had a no-op standing in for one.
##
## The protocol is line oriented at 9600 8N1. Write "H1\n" to capture a new
## zero at the current reading, "H7\n" to read the cell. Replies look like
##
##     command H1 ok. 8511666
##     command H7 ok. 8511562 0 g
##
## i.e. a raw count, and for H7 a converted weight. The tare is verified by
## reading back rather than trusted: a tare that silently failed leaves the
## same permanently triggered probe it was meant to fix, and failing loudly
## here is far cheaper to diagnose than a probe error several macros later.
##
## Tare with nothing touching the nozzle. Callers arrange that already - a
## clean ends parked clear of the bed - but a tare taken against the plate
## teaches the cell that contact is zero, which is worse than drift.

import logging

TARE_COMMAND = b"H1\n"
READ_COMMAND = b"H7\n"


def parse_weight(line):
    """Grams from an H7 reply, or None if this is not one.

    Kept separate from the I/O so the wire format can be tested without a
    printer attached. Replies are ASCII and end CRLF; a truncated or garbled
    read is None rather than an exception, so the caller can retry.
    """
    if not line:
        return None
    text = line.decode("utf-8", "ignore").strip() if isinstance(line, bytes) \
        else line.strip()
    if "ok." not in text:
        return None
    fields = text.split()
    ## "command H7 ok. <raw> <grams> g" - take the value before the unit
    ## rather than a fixed index, so an added field does not silently shift
    ## which number is read as the weight.
    for index, field in enumerate(fields):
        if field == "g" and index:
            try:
                return float(fields[index - 1])
            except ValueError:
                return None
    return None


class AD5XLoadCell:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.port = config.get("port", "/dev/ttyS7")
        self.baud = config.getint("baud", 9600)
        ## What counts as zeroed. The cell reads in whole grams, so a couple
        ## of grams is measurement noise; 140 was the drift that started this.
        self.tolerance_g = config.getfloat("tolerance", 5.0, minval=0.0)
        self.timeout = config.getfloat("timeout", 2.0, above=0.0)
        self.reads = config.getint("verify_reads", 3, minval=1)
        self.gcode.register_command(
            "LOAD_CELL_TARE", self.cmd_LOAD_CELL_TARE,
            desc="Zero the load cell the probe reads through")

    def _exchange(self, serial_port, command):
        serial_port.reset_input_buffer()
        serial_port.write(command)
        serial_port.flush()
        return serial_port.readline()

    def tare(self):
        """Capture a new zero and return the verified weight, in grams."""
        import serial  # deferred: only this board has the port

        with serial.Serial(self.port, self.baud, timeout=self.timeout) as port:
            reply = self._exchange(port, TARE_COMMAND)
            logging.info("ad5x_load_cell: tare reply %r", reply)
            weight = None
            for _ in range(self.reads):
                weight = parse_weight(self._exchange(port, READ_COMMAND))
                if weight is not None and abs(weight) <= self.tolerance_g:
                    return weight
            return weight

    def cmd_LOAD_CELL_TARE(self, gcmd):
        try:
            weight = self.tare()
        except Exception as failure:
            ## An unreadable cell is not something to print through: the probe
            ## would either dive or refuse, and both are worse than stopping.
            raise gcmd.error("Load cell tare failed on %s: %s"
                             % (self.port, failure))
        if weight is None:
            raise gcmd.error(
                "Load cell did not answer on %s; probing would be unsafe"
                % self.port)
        if abs(weight) > self.tolerance_g:
            raise gcmd.error(
                "Load cell still reads %.0f g after taring. Something is "
                "resting on the nozzle or the bed - clear it and retry."
                % weight)
        gcmd.respond_info("Load cell tared (%.0f g)" % weight)


def load_config(config):
    return AD5XLoadCell(config)
