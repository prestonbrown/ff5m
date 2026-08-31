## [gcode_macro LOAD_CELL_TARE]
##
## Copyright (C) 2025, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license


import logging
import time


## The cell is the same part on both boards and the tare is the same operation
## on both - "H1" - but it is reached differently. On the AD5M the conditioning
## MCU's H1 line is a GPIO the host toggles; on the AD5X it is a command sent
## over a UART. Weight comes back the same way round: an ADC-backed temperature
## sensor there, an "H7" query here. Only those two verbs differ, so they are
## the seam and everything above them - the threshold, the contact guard, the
## retries, cancelling a print whose tare cannot be trusted - is shared.


class PinTareBackend:
    """AD5M: H1 and the confirmation latch are GPIO lines."""

    ## The MCU calls the probe triggered above this, so a reading this high
    ## before taring means the nozzle is resting on something.
    contact_grams = 200

    def __init__(self, printer, run_gcode):
        self._printer = printer
        self._run_gcode = run_gcode

    def bind(self):
        self._weight = self._printer.lookup_object("temperature_sensor weightValue")
        self._level_pin = self._printer.lookup_object("gcode_button check_level_pin")

    def read_weight(self):
        return self._weight.last_temp

    def confirmed(self):
        return bool(self._level_pin.last_state)

    def tare(self):
        logging.info("LOAD_CELL_TARE: Send tare request.")
        # Tare is set by toggling level_h1 pin
        timeout = 250
        self._run_gcode(
            "SET_PIN PIN=level_h1 VALUE=0",
            f"WAIT TIME={timeout}",
            "SET_PIN PIN=level_h1 VALUE=1",
            f"WAIT TIME={timeout}",
            "SET_PIN PIN=level_h1 VALUE=0",
            f"WAIT TIME={timeout}",
            "SET_PIN PIN=level_h1 VALUE=1",
            f"WAIT TIME={timeout}",
        )

    def reset_confirmation(self):
        logging.info("LOAD_CELL_TARE: Reset tare confirmation.")
        # Toggle level clear pins
        # This action resets level_pin, which we read later to confirm that the tare was reset.
        timeout = 10
        self._run_gcode(
            "SET_PIN PIN=level_clear VALUE=0",
            f"WAIT TIME={timeout}",
            "SET_PIN PIN=level_clear VALUE=1",
            f"WAIT TIME={timeout}",
        )


def parse_weight(line):
    """Grams from an H7 reply, or None if this is not one.

    Kept out of the backend so the wire format is testable with no printer
    attached. A truncated or garbled read is None rather than an exception, so
    the caller retries instead of failing the tare.
    """
    if not line:
        return None
    text = line.decode("utf-8", "ignore") if isinstance(line, bytes) else line
    if "ok." not in text:
        return None
    fields = text.strip().split()
    ## "command H7 ok. <raw> <grams> g" - locate the value by its unit rather
    ## than by column, so an added field cannot shift which number is read as
    ## the weight. The H1 reply has a raw count and NO unit, and must not parse
    ## as a weight: reading it as one would report eight million grams.
    for index, field in enumerate(fields):
        if field == "g" and index:
            try:
                return float(fields[index - 1])
            except ValueError:
                return None
    return None


class SerialTareBackend:
    """AD5X: the same H1/H7 commands, over a UART.

    There is no confirmation latch on this board. `confirmed()` can only say
    that the cell answers and now reads near zero, which H1 makes true whatever
    state it was taken in - so it proves the tare executed, not that it was
    taken against clear air. The contact guard upstream carries that instead,
    which is why contact_grams is None here: measured drift on this board has
    reached 140 g with the nozzle in free air, above the threshold that would
    be needed to catch genuine contact, so weight cannot tell the two apart and
    the guard has to be positional.
    """

    contact_grams = None

    def __init__(self, printer, run_gcode, port, baud, tolerance, timeout):
        self._printer = printer
        self._run_gcode = run_gcode
        self._port = port
        self._baud = baud
        self._tolerance = tolerance
        self._timeout = timeout
        self._last_weight = 0.0
        self._handle = None

    def bind(self):
        pass

    def _link(self):
        ## One port, opened on first use and kept: the weight sensor polls
        ## every second or so, and re-running termios setup per exchange would
        ## churn it tens of thousands of times a day for no benefit. Klippy is
        ## single-threaded, so exchanges cannot interleave.
        if self._handle is None:
            import serial  # deferred: only this board has the port

            self._handle = serial.Serial(self._port, self._baud, timeout=self._timeout)
        return self._handle

    def _drop_link(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except Exception:
            logging.exception("LOAD_CELL_TARE: closing a failed serial link.")

    def _exchange(self, command):
        port = self._link()
        try:
            port.reset_input_buffer()
            port.write(command)
            port.flush()
            return port.readline()
        except Exception:
            ## The next exchange reopens the port. A tare still raises, since
            ## its caller must not trust a cell it could not read.
            self._drop_link()
            raise

    def read_weight(self):
        weight = parse_weight(self._exchange(b"H7\n"))
        if weight is not None:
            self._last_weight = weight
        return self._last_weight

    def confirmed(self):
        return abs(self.read_weight()) <= self._tolerance

    def tare(self):
        logging.info("LOAD_CELL_TARE: Send tare request over %s.", self._port)
        logging.info("LOAD_CELL_TARE: tare reply %r", self._exchange(b"H1\n"))

    def reset_confirmation(self):
        ## Nothing latches on this board, so there is nothing to clear.
        pass


class SerialWeightSensor:
    """The value half of this board's collision watchdog.

    Forge-X hangs bed-collision protection off a [temperature_sensor
    weightValue] whose exceed_gcode fires past a trigger value; the patched
    temperature_sensor polls whatever object its sensor_type built. On the
    AD5M that object is an ADC reading the cell under the bed. Here the cell
    is in the toolhead, behind the conditioning MCU's UART, so this sensor
    polls H7 through the shared tare backend - one owner for the port, one
    implementation of the wire format - and hands grams to the same callback.
    Grams stand in for degrees exactly as on the AD5M.

    report_time trades detection latency for reactor load: each poll is a
    blocking exchange on a 9600-baud link (~30 ms) with klippy idle
    throughout.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self._report_time = config.getfloat("report_time", 1.0, above=0.)
        self._tare = None
        self._callback = None
        self.printer.register_event_handler("klippy:ready", self._ready)

    def setup_minmax(self, min_temp, max_temp):
        ## Range is the exceed_gcode's decision to make, not this one's.
        pass

    def setup_callback(self, temperature_sensor_callback):
        self._callback = temperature_sensor_callback

    def _ready(self):
        self._tare = self.printer.lookup_object("load_cell_tare", None)
        if self._tare is None:
            logging.warning(
                "load_cell_serial: no [load_cell_tare] section; "
                "weight will not be published.")
            return
        self.reactor.register_timer(self._poll, self.reactor.NOW)

    def _poll(self, eventtime):
        try:
            self._callback(eventtime, self._tare.read_weight())
        except Exception:
            ## A watchdog must outlive the link it watches. A failed poll
            ## publishes nothing this round; the backend has already dropped
            ## the port, so the next poll retries on a fresh one.
            logging.exception("load_cell_serial: weight poll failed.")
        return eventtime + self._report_time


class LoadCellTareGcode:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")

        self.printer.register_event_handler("klippy:ready", self._init)

        ## Default is the GPIO transport, so a board that says nothing behaves
        ## exactly as it did before this seam existed.
        transport = config.get("transport", "pins")
        if transport == "serial":
            self.backend = SerialTareBackend(
                self.printer, self._run_gcode,
                config.get("port", "/dev/ttyS7"),
                config.getint("baud", 9600),
                config.getfloat("tolerance", 5.0, minval=0.0),
                config.getfloat("timeout", 2.0, above=0.0))
        elif transport == "pins":
            self.backend = PinTareBackend(self.printer, self._run_gcode)
        else:
            raise config.error(
                "Unknown load_cell_tare transport '%s' (expected pins or serial)"
                % transport)

        self.gcode.register_command("LOAD_CELL_TARE", self.cmd_LOAD_CELL_TARE)

    def _init(self):
        self.toolhead = self.printer.lookup_object("toolhead")
        self.probe = self.printer.lookup_object("probe")
        self.mod_params = self.printer.lookup_object("mod_params")
        self.backend.bind()

    def _run_gcode(self, *cmds: str):
        self.gcode.run_script_from_command("\n".join(cmds))

    def _tare_confirmed(self):
        return self.backend.confirmed()

    def read_weight(self):
        ## Current grams from the active backend. The serial weight sensor
        ## publishes these into klippy's temperature_sensor machinery.
        return self.backend.read_weight()

    def cmd_LOAD_CELL_TARE(self, gcmd):
        t = time.time()

        weight = self.backend.read_weight()
        threshold_weight = self.mod_params.variables.get("cell_weight", 0)

        if weight < threshold_weight:
            logging.info(f"LOAD_CELL_TARE: Skipped (weight {weight} within threshold {threshold_weight})")
            return

        logging.info(f"LOAD_CELL_TARE: Started load cell tare. Weight: {weight}, threshold: {threshold_weight}")

        # Check tare confirmation state reset
        for i in range(5):
            if not self._tare_confirmed():
                break

            gcmd.respond_info(f"Attempt {i + 1}. Tare conformation is not clear. Try to reset...")
            self._reset_tare_confirmation()
            self._run_gcode("WAIT TIME=100")
        else:
            return self._raise_error("Tare conformation did not reset.")

        # Check bed pressure to ensure no toolhead contact
        # Taring in that case would be incorrect
        self._query_probe()

        # Try to tare several times, as events may not be received in a single attempt
        ok = False
        for i in range(5):
            self._cell_tare()
            if self._tare_confirmed():
                ok = True
                break

            logging.info(f"LOAD_CELL_TARE: Attempt {i + 1}. No confirmation from level sensor. Weight: {self.backend.read_weight()}")

        self._reset_tare_confirmation()

        if ok:
            logging.info("LOAD_CELL_TARE: Tare confirmed.")

            self._run_gcode("WAIT TIME=100")
            settled = self.backend.read_weight()
            if abs(settled) > threshold_weight:
                return self._raise_error(f"Load cell tare failed: weight {settled} > threshold {threshold_weight}")

        elif self.config.getint("skip_tare_error", 0):
            return self._raise_error("Load cell tared, but no confirmation from level_pin; configured to skip.")

        else:
            return self._raise_error("Load cell tare failed. No tare confirmation received")

        # If we are here - tare is considered successful
        logging.info(f"LOAD_CELL_TARE: Load cell tare finished in {time.time() - t:0.1f}s.")

    def _query_probe(self):
        # This may trigger a "Timer too close" error.
        # self._run_gcode("QUERY_PROBE")

        # Instead, we simply check the weight value since the MCU handles this in QUERY_PROBE.
        # It checks if the weight is greater than 200; if so, the probe is considered triggered

        ## A board whose drift can exceed any threshold low enough to catch
        ## contact cannot answer this from weight, so it clears Z every time
        ## rather than deciding. Taring against the plate teaches the cell that
        ## contact reads zero, and then nothing stops the nozzle until it is
        ## pushing hard - the expensive failure, so guess in the safe direction.
        if self.backend.contact_grams is not None:
            weight = self.backend.read_weight()
            if weight < self.backend.contact_grams:
                logging.info("LOAD_CELL_TARE: No pressure to bed detected. OK!")
                return

            logging.info("LOAD_CELL_TARE: Detected bed pressure.")
            self.gcode.respond_raw("!! Detected bed pressure. Please ensure the bed is clean!")

        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=CELL_TARE")

        kin_status = self.toolhead.get_kinematics().get_status(0)
        safe_z = abs(float(self.mod_params.variables.get("safe_z", 10.0)))
        if "z" not in kin_status['homed_axes']:
            logging.info("LOAD_CELL_TARE: Start Z homing...")
            self._run_gcode(
                "G28 Z",
                "M400"
            )
        elif self.toolhead.get_position()[2] < safe_z:  # position.z
            logging.info("LOAD_CELL_TARE: Moving bed lower...")
            self._run_gcode(
                "G90",
                "G1 Z%g F6000" % safe_z,
                "M400",
            )

        self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=CELL_TARE")

    def _raise_error(self, msg):
        start_print_vars = self.printer.lookup_object('gcode_macro _START_PRINT').variables
        if start_print_vars["print_active"]:
            if self.mod_params.variables['display'] != 0:
                self.gcode.run_script_from_command('CANCEL_PRINT REASON="Cell tare failed!"')
            else:
                self.gcode.run_script_from_command('CANCEL_PRINT')

        raise self.gcode.error(msg)

    def _cell_tare(self):
        self.backend.tare()

    def _reset_tare_confirmation(self):
        self.backend.reset_confirmation()


def load_config(config):
    ## Registering the sensor type here makes [load_cell_tare] the only
    ## section a board needs: instantiating it loads the plugin, which
    ## registers "load_cell_serial" for any [temperature_sensor] section
    ## further down the file to use. Klippy instantiates sections in file
    ## order, so that ordering is load-bearing.
    pheaters = config.get_printer().load_object(config, "heaters")
    pheaters.add_sensor_factory("load_cell_serial", SerialWeightSensor)
    return LoadCellTareGcode(config)
