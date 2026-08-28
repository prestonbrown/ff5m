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


class IfsBusy(Exception):
    """A queued command went unanswered, or there was nothing to answer it."""


class _Request(object):
    """One command handed to the poll thread, and the answer coming back."""

    __slots__ = ("command", "done", "response", "error")

    def __init__(self, command):
        self.command = command
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

        self._lock = threading.Lock()
        self._stopping = threading.Event()
        ## Wakes the poll thread the moment work arrives, so a queued command
        ## does not wait out the poll interval before it is sent.
        self._wake = threading.Event()
        self._queue = []
        self._queue_lock = threading.Lock()
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
            self._wake.wait(self.poll_interval)
            self._wake.clear()

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
        thread = self._thread
        if thread is None or not thread.is_alive():
            raise IfsBusy("the IFS poller is not running")
        request = _Request(command)
        with self._queue_lock:
            self._queue.append(request)
        self._wake.set()

        deadline = self.reactor.monotonic() + timeout
        while not request.done.is_set():
            if self.reactor.monotonic() > deadline:
                self._forget(request)
                raise IfsBusy("no answer to %s within %.1fs"
                              % (command, timeout))
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

    def is_stalled(self, channel=0):
        status = self.latest_status()
        return None if status is None else status.is_stalled(channel)

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
            "stalled_channels": [],
            "inserted_channels": [],
        }
        if status is not None:
            info.update({
                "state": status.state,
                "activity": status.activity_name,
                "activity_channel": status.activity_channel,
                "active_channel": status.active_channel,
                "loaded_channels": status.loaded_channels,
                "stalled_channels": status.stalled_channels,
                "inserted_channels": status.inserted_channels,
            })
        return info

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
            "IFS %s, %d channels: %s%s | loaded %s | stalled %s"
            % (info["version"], info["channel_count"], info["activity"], where,
               info["loaded_channels"] or "none",
               info["stalled_channels"] or "none"))

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
