## Waiting for the IFS board to finish, or to tell us why it will not.
##
## The board answers a motion command immediately and then works; what actually
## happened only shows up in the F13 stream afterwards. So every step of a load
## or an unload is "send it, then watch until one of a handful of things
## becomes true", and this is that watching, with no klipper and no serial in
## it so the decisions can be tested on their own.
##
## Semantics follow ghzserg's wait_for_state, which is the only working
## reference: a sensor condition is only meaningful while the board is in the
## state the command put it in, and it has to hold for several consecutive
## polls before it counts. A single sample is noise - measured on hardware, a
## clean 20 mm retract sets the stall bit in passing.
##
## Copyright (C) 2026, Preston Brown
## Portions derived from zmod (C) 2025-2026 ghzserg <https://github.com/ghzserg/zmod/>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from . import ifs_status


## How a wait ended.
WAITING = "waiting"            # nothing decisive yet
FINISHED = "finished"          # the board went back to ready: motion completed
FILAMENT = "filament"          # the channel's filament sensor reached the wanted state
STALLED = "stalled"            # filament stopped moving
DRIVER_ERROR = "driver_error"  # the board wants F15 before anything else
TIMED_OUT = "timed_out"

## Outcomes that mean "stop and deal with it", as opposed to a normal finish.
PROBLEMS = frozenset([STALLED, DRIVER_ERROR, TIMED_OUT])

## Consecutive matching polls before a sensor condition counts.
DEFAULT_CONFIRMATIONS = 3


class Outcome(object):
    """How a wait ended, and what the board looked like when it did."""

    __slots__ = ("kind", "status", "elapsed", "detail")

    def __init__(self, kind, status=None, elapsed=0.0, detail=None):
        self.kind = kind
        self.status = status
        self.elapsed = elapsed
        self.detail = detail

    @property
    def is_problem(self):
        return self.kind in PROBLEMS

    def __repr__(self):
        return "Outcome(%s%s)" % (self.kind,
                                  ", %s" % self.detail if self.detail else "")


class StateWaiter(object):
    """Decides, poll by poll, whether a commanded move is done.

    Feed it every F13 reading. It returns an Outcome each time; keep going
    while that is WAITING.

    `expect_filament` is what the channel's filament bit should become for the
    move to count as successful - True for a load, False for an unload. It is
    only tested while the board is in `activity`, because outside that state
    the bit is describing something we did not ask for.
    """

    def __init__(self, channel, activity=None, expect_filament=None,
                 watch_stall=True, confirmations=DEFAULT_CONFIRMATIONS):
        if confirmations < 1:
            raise ValueError("confirmations must be at least 1")
        self.channel = channel
        self.activity = activity
        self.expect_filament = expect_filament
        self.watch_stall = watch_stall
        self.confirmations = confirmations
        self._filament_run = 0
        self._stall_run = 0
        self._seen_activity = False

    @property
    def expected_state(self):
        """The wire value F13 should report while this move is running."""
        if self.activity is None:
            return None
        return ifs_status.state_value(self.activity, self.channel)

    def update(self, status, elapsed=0.0):
        if status is None:
            return Outcome(WAITING, None, elapsed, "no reading")

        if status.is_driver_error:
            return Outcome(DRIVER_ERROR, status, elapsed,
                           "board reports a driver fault; F15 required")

        in_activity = (self.activity is None
                       or (status.activity == self.activity
                           and status.activity_channel == self.channel))
        if in_activity:
            self._seen_activity = True
            outcome = self._check_sensors(status, elapsed)
            if outcome is not None:
                return outcome
        else:
            ## Conditions only count while the board is doing what we asked.
            self._filament_run = self._stall_run = 0

        if status.is_ready:
            ## Ready before the board ever entered the activity means the move
            ## has not started yet, not that it finished.
            if self._seen_activity or self.activity is None:
                return Outcome(FINISHED, status, elapsed)
        return Outcome(WAITING, status, elapsed)

    def _check_sensors(self, status, elapsed):
        if self.expect_filament is not None:
            if status.has_filament(self.channel) == self.expect_filament:
                self._filament_run += 1
                if self._filament_run >= self.confirmations:
                    return Outcome(FILAMENT, status, elapsed,
                                   "filament %s on channel %d"
                                   % ("present" if self.expect_filament
                                      else "gone", self.channel))
            else:
                self._filament_run = 0

        if self.watch_stall:
            if status.is_stalled(self.channel):
                self._stall_run += 1
                if self._stall_run >= self.confirmations:
                    return Outcome(STALLED, status, elapsed,
                                   "channel %d stopped moving" % self.channel)
            else:
                self._stall_run = 0
        return None

    def timed_out(self, status=None, elapsed=0.0):
        return Outcome(TIMED_OUT, status, elapsed,
                       "board never reached %s"
                       % ifs_status.activity_name(self.activity)
                       if self.activity is not None else "board never finished")
