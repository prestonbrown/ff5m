## Klipper object for the AD5X IFS (4-channel filament system).
##
##     [ifs]
##     port: /dev/ttyS4
##     poll_interval: 1.0
##
## Owns the serial link and publishes what it sees. The framing, the parsing and
## the operations live in ifs_link / ifs_status / ifs_operations, which have no
## klipper in them and are tested without a printer.
##
## Threading, both directions:
##
##   * Klipper's reactor is single-threaded and one F13 exchange takes ~165 ms on
##     real hardware. Polling from a reactor timer would stall klipper for that
##     long every second, so the serial work happens on a daemon thread. The only
##     shared state is one snapshot behind a lock, and anything that touches
##     klipper is marshalled back with reactor.register_async_callback.
##
##   * Commands from klipper go the other way, through a queue. IfsLink is not
##     thread-safe, and a plain mutex would be worse than useless: the reactor
##     would block for up to a full exchange while the poller finished one,
##     repeatedly, during exactly the sequences that matter. So gcode submits a
##     request, the poll thread runs it between polls, and the caller yields the
##     reactor with reactor.pause() until the answer lands.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import logging
import threading

from . import ifs_diagnostics
from . import ifs_link
from . import ifs_operations
from . import ifs_sequences
from . import ifs_status


## How long to wait before rebuilding a link that failed. The board is not going
## anywhere; retrying in a tight loop only fills the log.
RECONNECT_DELAY = 5.0

## Consecutive failed polls before the link is torn down and rebuilt. One bad
## read is noise; a run of them means the port is gone.
MAX_POLL_FAILURES = 3

## How long a queued command may go unanswered before the caller gives up.
## Generous: the board answers when it accepts the command, not when the motion
## finishes, but a busy board can take its time.
DEFAULT_COMMAND_TIMEOUT = 15.0

## How often the reactor-side waiter looks for its answer. Short enough to feel
## immediate, long enough not to spin.
COMMAND_POLL_PAUSE = 0.05

## A move can take a while: a metre of filament at 1200 mm/min is fifty
## seconds, and a cold-ish nozzle makes a purge slower still.
DEFAULT_MOVE_TIMEOUT = 120.0
## How long to let the board finish acting on a command it already accepted.
## Clamping is mechanical and quick; this only has to outlast that.
SETTLE_TIMEOUT = 15.0
## What zmod polls F13 at inside wait_for_state (its HOST_REPORT_TIME). The
## background cadence is fine for status but far too coarse to judge motion:
## the board's motion bit toggles, so three 1s samples can all land in gaps
## while the motor is running and read as a stall that never happened.
MOVE_POLL_INTERVAL = 0.2

## What a move waits for, as the UNTIL= parameter spells it.
UNTIL_TOOLHEAD = "toolhead"
UNTIL_CLEAR = "clear"
UNTIL_DONE = "done"


class IfsBusy(Exception):
    """A queued command went unanswered, or there was nothing to answer it."""


class _Request(object):
    """One command handed to the poll thread, and the answer coming back."""

    __slots__ = ("command", "action", "done", "response", "error")

    def __init__(self, command, action=None):
        ## `command` is always the label used in errors. When `action` is set it
        ## is run against IfsOperations instead of being written to the link, so
        ## a caller gets the operations layer's payload checking rather than an
        ## unvalidated write.
        self.command = command
        self.action = action
        self.done = threading.Event()
        self.response = None
        self.error = None

    def settle(self, response=None, error=None):
        self.response = response
        self.error = error
        self.done.set()


def _open_link(port, send_commit_byte):
    """Build and open the serial link. The seam tests replace."""
    link = ifs_link.IfsLink(send_commit_byte=send_commit_byte)
    link.open()
    return link


class IFS(object):
    def __init__(self, config, open_link=_open_link):
        self._open_link = open_link
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()

        self.poll_interval = config.getfloat("poll_interval", 1.0, above=0.1)
        self.port = config.get("port", ifs_link.PORT)
        self.send_commit_byte = config.getboolean(
            "send_commit_byte", ifs_link.SEND_COMMIT_BYTE)
        ## Only used until F19 answers, and kept by a board that never does.
        self.configured_channels = config.getint(
            "channel_count", ifs_status.MAX_CHANNELS, minval=1, maxval=8)
        ## Which filament_switch_sensor speaks for the toolhead. UNTIL=toolhead
        ## has nothing to wait on without it.
        self.toolhead_sensor_name = config.get("toolhead_sensor", None)
        self.params = ifs_sequences.Parameters(
            tube_mm=config.getfloat("tube_length", 1000.0, above=0.),
            ifs_speed=config.getfloat("ifs_speed", 1200.0, above=0.))

        self._lock = threading.Lock()
        self._stopping = threading.Event()
        ## Wakes the poll thread the moment work arrives, so a queued command
        ## does not wait out the poll interval before it is sent.
        self._wake = threading.Event()
        self._queue = []
        self._queue_lock = threading.Lock()
        ## How many callers are watching a move right now. Non-zero means the
        ## poll thread runs at zmod's cadence instead of the idle one.
        self._watchers = 0
        self._thread = None
        self._link = None
        self._ops = None
        self._failures = 0
        self._inserts = ifs_status.InsertWatcher()

        ## Guarded by _lock.
        self._status = None
        self._capabilities = None
        self._connected = False
        self._error = None

        self.printer.register_event_handler("klippy:ready", self._start)
        for event in ("klippy:disconnect", "klippy:shutdown"):
            self.printer.register_event_handler(event, self._stop)

        gcode = self.printer.lookup_object("gcode")
        gcode.register_command("IFS_STATUS", self.cmd_IFS_STATUS,
                               desc="Report the IFS board's current state")
        gcode.register_command("IFS_DIAGNOSTICS", self.cmd_IFS_DIAGNOSTICS,
                               desc="Report IFS firmware, stall counts and "
                                    "stepper driver registers")
        ## Primitives, each blocking until its move is decided. The sequences
        ## that use them live in macros, so a user can read, run and override
        ## them from the console.
        for name, handler, description in (
                ("IFS_CLAMP", self.cmd_IFS_CLAMP,
                 "Clamp a channel and wait for the board to confirm"),
                ("IFS_RELEASE", self.cmd_IFS_RELEASE, "Release a channel"),
                ("IFS_RELEASE_ALL", self.cmd_IFS_RELEASE_ALL,
                 "Release every channel"),
                ("IFS_FEED", self.cmd_IFS_FEED,
                 "Feed filament towards the toolhead until the sensor sees it"),
                ("IFS_RETRACT", self.cmd_IFS_RETRACT,
                 "Pull filament back into its lane"),
                ("IFS_STOP", self.cmd_IFS_STOP, "Stop the board feeding"),
                ("IFS_MARK_INSERTED", self.cmd_IFS_MARK_INSERTED,
                 "Tell the board a channel now holds filament"),
                ("IFS_RESET_DRIVER", self.cmd_IFS_RESET_DRIVER,
                 "Reset the board's stepper driver after a fault")):
            gcode.register_command(name, handler, desc=description)

    ## -- lifecycle ----------------------------------------------------------

    def _start(self):
        self._thread = threading.Thread(target=self._poll_loop, name="ifs-poll")
        self._thread.daemon = True
        self._thread.start()

    def _stop(self):
        self._stopping.set()
        self._wake.set()
        self._fail_queued("the IFS poller is shutting down")
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        self._drop_link()

    ## -- the polling thread -------------------------------------------------

    def _poll_loop(self):
        while not self._stopping.is_set():
            if self._link is None and not self._connect():
                self._fail_queued("the IFS is not connected")
                self._stopping.wait(RECONNECT_DELAY)
                continue
            ## Queued commands come first and skip the poll interval: a
            ## sequence waiting on one should not pay for a status poll.
            if self._run_queued():
                continue
            if self._poll_once():
                self._failures = 0
            else:
                self._failures += 1
                if self._failures >= MAX_POLL_FAILURES:
                    self._drop_link("%d consecutive poll failures"
                                    % self._failures)
            self._wake.wait(self._poll_delay())
            self._wake.clear()

    def _poll_delay(self):
        with self._lock:
            return MOVE_POLL_INTERVAL if self._watchers else self.poll_interval

    def _watch_moves(self, delta):
        with self._lock:
            self._watchers += delta
        if delta > 0:
            ## Do not make the watcher wait out the idle interval first.
            self._wake.set()

    def _connect(self):
        try:
            link = self._open_link(self.port, self.send_commit_byte)
        except Exception as exc:
            self._note_error("cannot open %s: %s" % (self.port, exc))
            return False
        capabilities = link.capabilities
        channels = (capabilities.channel_count
                    if capabilities is not None and capabilities.probed
                    else self.configured_channels)
        self._link = link
        self._ops = ifs_operations.IfsOperations(link, channel_count=channels)
        self._failures = 0
        with self._lock:
            self._capabilities = capabilities
            self._connected = True
            self._error = None
        logging.info("IFS: connected on %s, %d channels, firmware %s%s",
                     self.port, channels,
                     capabilities.version if capabilities else None,
                     "" if capabilities and capabilities.probed
                     else " (F19 unanswered)")
        return True

    def _poll_once(self):
        """One F13. False on any failure; the loop decides what that costs."""
        try:
            status = self._ops.poll_status()
        except Exception as exc:
            self._note_error(str(exc))
            return False
        with self._lock:
            self._status = status
            self._connected = True
            self._error = None
        inserted = self._inserts.update(status)
        if inserted:
            self.reactor.register_async_callback(
                lambda eventtime, ch=inserted: self.printer.send_event(
                    "ifs:filament_inserted", ch))
        return True

    def _drop_link(self, reason=None):
        if reason:
            logging.warning("IFS: dropping the link: %s", reason)
        link, self._link, self._ops = self._link, None, None
        if link is None:
            return
        try:
            link.close()
        except Exception as exc:
            logging.warning("IFS: closing the link failed: %s", exc)

    def _note_error(self, message):
        logging.info("IFS: %s", message)
        with self._lock:
            self._connected = False
            self._error = message

    ## -- commands from klipper ----------------------------------------------

    def _take_queued(self):
        with self._queue_lock:
            return self._queue.pop(0) if self._queue else None

    def _run_queued(self):
        """Run one queued command. True when there was one to run."""
        request = self._take_queued()
        if request is None:
            return False
        try:
            if request.action is not None:
                request.settle(response=request.action(self._ops))
            else:
                request.settle(response=self._link.request(request.command))
        except Exception as exc:
            request.settle(error=str(exc))
            self._note_error("%s failed: %s" % (request.command, exc))
        return True

    def _fail_queued(self, reason):
        """Answer everything waiting, so no caller hangs on a dead link."""
        while True:
            request = self._take_queued()
            if request is None:
                return
            request.settle(error=reason)

    def _forget(self, request):
        with self._queue_lock:
            if request in self._queue:
                self._queue.remove(request)

    def execute(self, command, timeout=DEFAULT_COMMAND_TIMEOUT):
        """Run one IFS command from the reactor thread.

        Yields the reactor while waiting rather than blocking it, so klipper
        keeps servicing its MCUs through a sequence that takes tens of seconds.
        Raises IfsBusy when nothing answers, RuntimeError when the board or the
        link did.
        """
        return self._submit(_Request(command), command, timeout)

    def run_operation(self, label, action, timeout=DEFAULT_COMMAND_TIMEOUT):
        """Run one IfsOperations call on the poll thread.

        The operations layer checks the board's reply payload, which is the only
        thing that says whether a command happened - the board prefixes refusals
        with "ok." exactly as it prefixes successes.
        """
        return self._submit(_Request(label, action=action), label, timeout)

    def _submit(self, request, label, timeout):
        thread = self._thread
        if thread is None or not thread.is_alive():
            raise IfsBusy("the IFS poller is not running")
        with self._queue_lock:
            self._queue.append(request)
        self._wake.set()

        deadline = self.reactor.monotonic() + timeout
        while not request.done.is_set():
            if self.reactor.monotonic() > deadline:
                self._forget(request)
                raise IfsBusy("no answer to %s within %.1fs"
                              % (label, timeout))
            self.reactor.pause(self.reactor.monotonic() + COMMAND_POLL_PAUSE)
        if request.error is not None:
            raise RuntimeError(request.error)
        return request.response

    ## -- readers ------------------------------------------------------------

    def latest_status(self):
        with self._lock:
            return self._status

    @property
    def capabilities(self):
        with self._lock:
            return self._capabilities

    @property
    def channel_count(self):
        capabilities = self.capabilities
        if capabilities is not None and capabilities.probed:
            return capabilities.channel_count
        return self.configured_channels

    def has_filament(self, channel):
        """True/False per channel, or None before the first reading.

        None is deliberately distinct from False: a sensor that has not read yet
        must not look like a runout.
        """
        status = self.latest_status()
        return None if status is None else status.has_filament(channel)

    def is_moving(self, channel=0):
        """Whether filament is moving, or None before the first reading."""
        status = self.latest_status()
        return None if status is None else status.is_moving(channel)

    def get_status(self, eventtime=None):
        with self._lock:
            status, connected, error = self._status, self._connected, self._error
            capabilities = self._capabilities
        info = {
            "connected": connected,
            "error": error,
            "channel_count": self.channel_count,
            "version": capabilities.version if capabilities else None,
            "probed": bool(capabilities and capabilities.probed),
            "state": None,
            "activity": None,
            "activity_channel": None,
            "active_channel": None,
            "loaded_channels": [],
            "moving_channels": [],
            "inserted_channels": [],
            ## Macros own the choreography, so they need the numbers. Sourced
            ## from the printer's own Multicolour block where available.
            "params": self.params.as_dict(),
        }
        if status is not None:
            info.update({
                "state": status.state,
                "activity": status.activity_name,
                "activity_channel": status.activity_channel,
                "active_channel": status.active_channel,
                "loaded_channels": status.loaded_channels,
                "moving_channels": status.moving_channels,
                "inserted_channels": status.inserted_channels,
            })
        return info

    ## -- moves, with the wait that decides whether they worked ------------

    def _toolhead_sensor(self):
        if not self.toolhead_sensor_name:
            return None
        return self.printer.lookup_object(
            "filament_switch_sensor %s" % self.toolhead_sensor_name, None)

    def _toolhead_has_filament(self):
        """True/False/None from the toolhead sensor, None when we cannot ask."""
        sensor = self._toolhead_sensor()
        if sensor is None or not hasattr(sensor, "read_present"):
            return None
        return sensor.read_present()

    def _fresh_status(self, previous):
        """The newest poll, skipping the one that predates our command."""
        status = self.latest_status()
        return None if status is previous else status

    def _await(self, gcmd, waiter, timeout, until=None):
        """Watch until the move ends, yielding the reactor throughout.

        Reads the poller's snapshots rather than issuing its own F13s: the
        poller is already asking, and doubling the traffic would only slow both.
        """
        started = self.reactor.monotonic()
        deadline = started + timeout
        before = self.latest_status()
        self._watch_moves(1)
        try:
            return self._await_loop(gcmd, waiter, deadline, started, before,
                                    until)
        finally:
            self._watch_moves(-1)

    def _await_loop(self, gcmd, waiter, deadline, started, before, until):
        while self.reactor.monotonic() < deadline:
            self.reactor.pause(self.reactor.monotonic() + COMMAND_POLL_PAUSE)
            elapsed = self.reactor.monotonic() - started

            if until == UNTIL_TOOLHEAD and self._toolhead_has_filament() is True:
                return ifs_sequences.Outcome(
                    ifs_sequences.FILAMENT, self.latest_status(), elapsed,
                    "toolhead sensor sees filament")
            if until == UNTIL_CLEAR and self._toolhead_has_filament() is False:
                return ifs_sequences.Outcome(
                    ifs_sequences.FILAMENT, self.latest_status(), elapsed,
                    "toolhead sensor is clear")

            status = self._fresh_status(before)
            if status is None:
                continue
            before = status
            outcome = waiter.update(status, elapsed)
            if outcome.kind != ifs_sequences.WAITING:
                return outcome
        return waiter.timed_out(self.latest_status(),
                                self.reactor.monotonic() - started)

    def _finish(self, gcmd, outcome, what):
        """Report an outcome, and stop the board if it was a bad one."""
        if not outcome.is_problem:
            gcmd.respond_info("%s: %s%s"
                              % (what, outcome.kind,
                                 " (%s)" % outcome.detail if outcome.detail
                                 else ""))
            return outcome
        ## Leaving a jammed board feeding is how filament gets ground away.
        try:
            self.execute("F112")
        except Exception as exc:
            logging.warning("IFS: could not stop after %s: %s",
                            outcome.kind, exc)
        raise gcmd.error("%s failed: %s%s"
                         % (what, outcome.kind,
                            " (%s)" % outcome.detail if outcome.detail else ""))

    def _channel(self, gcmd):
        return gcmd.get_int("CHANNEL", minval=1, maxval=self.channel_count)

    def cmd_IFS_CLAMP(self, gcmd):
        ## Completes on the board's acknowledgement, NOT on a state transition.
        ## F24 answers "F24 ok. chan N." and the board stays in `ready`; it
        ## never reports CLAMPED for the channel, so waiting for that state
        ## timed out every time while the clamp itself had already happened.
        ## zmod's cmd_IFS_F24 waits on the same acknowledgement.
        channel = self._channel(gcmd)
        self._run(gcmd, "F24 C%d" % channel, lambda ops: ops.clamp(channel))
        self._settle(gcmd, channel, "clamp channel %d" % channel)
        gcmd.respond_info("clamped channel %d" % channel)

    def cmd_IFS_RELEASE(self, gcmd):
        channel = self._channel(gcmd)
        self._run(gcmd, "F39 C%d" % channel,
                  lambda ops: ops.release(channel))
        self._settle(gcmd, channel, "release channel %d" % channel)
        gcmd.respond_info("released channel %d" % channel)

    def cmd_IFS_MARK_INSERTED(self, gcmd):
        ## zmod's IFS_F23, the last IFS step of its load. Without it the board
        ## is never told the lane is occupied, so its own view of which channels
        ## hold filament goes stale as soon as we load one.
        channel = self._channel(gcmd)
        self._run(gcmd, "F23 C%d" % channel,
                  lambda ops: ops.mark_inserted(channel))
        gcmd.respond_info("marked channel %d inserted" % channel)

    def cmd_IFS_RELEASE_ALL(self, gcmd):
        self._run(gcmd, "F18", lambda ops: ops.release_all())
        gcmd.respond_info("released every channel")

    def cmd_IFS_STOP(self, gcmd):
        self.execute("F112")
        gcmd.respond_info("stopped")

    def cmd_IFS_RESET_DRIVER(self, gcmd):
        ## The literal C is what the firmware expects; it is not a channel.
        self.execute("F15 C")
        gcmd.respond_info("driver reset")

    def _settle(self, gcmd, channel, what, timeout=SETTLE_TIMEOUT):
        """Wait for the board to come back to READY after a command.

        zmod follows the acknowledgement of both F24 and F39 with
        wait_for_state(), which returns when F13 reports READY. The
        acknowledgement only says the opcode was accepted - the board is still
        acting on it, and the next opcode sent meanwhile is refused with "FFS
        not ready.". A feed issued straight after a clamp hit exactly that.
        """
        waiter = ifs_sequences.StateWaiter(channel, watch_stall=False)
        return self._finish(gcmd, self._await(gcmd, waiter, timeout), what)

    def _run(self, gcmd, label, action, timeout=DEFAULT_COMMAND_TIMEOUT):
        """run_operation, reporting board and link failures as command errors.

        Anything that is not a gcode error reaches klipper as "Internal error on
        command", which puts klippy into SHUTDOWN and takes the MCUs down with
        it. A board that refuses an opcode is an ordinary, expected answer - it
        must fail the command, not the printer. Observed the hard way: a refused
        F10 shut the printer down and needed a FIRMWARE_RESTART.
        """
        try:
            return self.run_operation(label, action, timeout)
        except gcmd.error:
            raise
        except Exception as exc:
            raise gcmd.error(str(exc))

    ## Which IfsOperations call each move opcode is. Going through operations
    ## rather than writing the opcode to the link is what checks the reply: the
    ## board prefixes a refusal with "ok." exactly as it prefixes a success, so
    ## an unvalidated send turns "F10 ok. FFS not ready." into a silent
    ## two-minute wait for a state that was never coming.
    MOVE_OPERATIONS = {"F10": "feed", "F11": "retract"}

    def _move(self, gcmd, opcode, activity, default_until, what):
        channel = self._channel(gcmd)
        length = gcmd.get_float("LENGTH", self.params.tube_mm, above=0.)
        speed = gcmd.get_float("SPEED", self.params.ifs_speed, above=0.)
        until = gcmd.get("UNTIL", default_until)
        timeout = gcmd.get_float("TIMEOUT", DEFAULT_MOVE_TIMEOUT, above=0.)

        if until in (UNTIL_TOOLHEAD, UNTIL_CLEAR):
            if self._toolhead_has_filament() is None:
                raise gcmd.error(
                    "UNTIL=%s needs a toolhead sensor; set toolhead_sensor= "
                    "in [ifs]" % until)
        elif until != UNTIL_DONE:
            raise gcmd.error("UNTIL must be %s, %s or %s"
                             % (UNTIL_TOOLHEAD, UNTIL_CLEAR, UNTIL_DONE))

        label = "%s C%d L%d S%d" % (opcode, channel, length, speed)
        operation = self.MOVE_OPERATIONS[opcode]
        self._run(gcmd, label,
                  lambda ops: getattr(ops, operation)(channel, length, speed))

        if gcmd.get_int("SLEEP", 0):
            ## zmod's SLEEP=1: fire the opcode and pause for a fixed fraction of
            ## the move rather than watching state at all. It is used where the
            ## EXTRUDER is driving the same filament, and there the lane's
            ## motion bit is not a stall signal - something else is pulling, and
            ## at the extruder's 300 mm/min the bit reads as stopped within
            ## seconds. Watching it there failed a co-push that was working.
            self.reactor.pause(self.reactor.monotonic()
                               + (length * 20) // speed + 1)
            return None

        waiter = ifs_sequences.StateWaiter(channel, activity)
        outcome = self._await(gcmd, waiter, timeout, until=until)
        return self._finish(gcmd, outcome,
                            "%s channel %d" % (what, channel))

    def cmd_IFS_FEED(self, gcmd):
        self._move(gcmd, "F10", ifs_status.LOADING, UNTIL_TOOLHEAD, "feed")

    def cmd_IFS_RETRACT(self, gcmd):
        self._move(gcmd, "F11", ifs_status.UNLOADING, UNTIL_DONE, "retract")

    ## -- gcode --------------------------------------------------------------

    def cmd_IFS_STATUS(self, gcmd):
        info = self.get_status()
        if not info["connected"]:
            gcmd.respond_info("IFS: not connected (%s)"
                              % (info["error"] or "no reason recorded"))
            return
        where = (" ch%d" % info["activity_channel"]
                 if info["activity_channel"] else "")
        gcmd.respond_info(
            "IFS %s, %d channels: %s%s | loaded %s | moving %s"
            % (info["version"], info["channel_count"], info["activity"], where,
               info["loaded_channels"] or "none",
               info["moving_channels"] or "none"))

    def cmd_IFS_DIAGNOSTICS(self, gcmd):
        if self._link is None:
            gcmd.respond_info("IFS: not connected")
            return
        try:
            diag = ifs_diagnostics.read_diagnostics(self._link, self.capabilities)
        except Exception as exc:
            gcmd.respond_info("IFS: diagnostics failed: %s" % exc)
            return
        gcmd.respond_info("IFS firmware %s, %s channels"
                          % (diag.version, diag.channel_count))
        if diag.stall_counts:
            gcmd.respond_info("  stall counts: %s" % (diag.stall_counts,))
        if diag.silk_raw:
            gcmd.respond_info("  raw silk (low = loaded): %s" % (diag.silk_raw,))
        for driver in diag.drivers:
            gcmd.respond_info("  %-8s %s" % (
                driver.label,
                ", ".join(driver.flags) if driver.flags else "no flags"))
        for label, fault in diag.faults:
            gcmd.respond_info("  !! %s: %s (%s)"
                              % (label, fault, ifs_diagnostics.describe(fault)))
        if not diag.faults:
            gcmd.respond_info("  no driver faults")


def load_config(config):
    return IFS(config)
