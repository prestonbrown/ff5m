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
FILAMENT = "filament"          # the TOOLHEAD sensor reached the wanted state
STALLED = "stalled"            # filament stopped moving
RUNOUT = "runout"              # the lane lost its filament mid-move
DRIVER_ERROR = "driver_error"  # the board wants F15 before anything else
TIMED_OUT = "timed_out"

## Outcomes that mean "stop and deal with it", as opposed to a normal finish.
PROBLEMS = frozenset([STALLED, RUNOUT, DRIVER_ERROR, TIMED_OUT])

## Consecutive matching polls before a condition counts. zmod keeps two
## separate counts and so do we: a stall has to persist (its motion bit toggles,
## so single samples read as stopped all the time), while the lane's own
## filament bit is steady and one reading of it is enough.
DEFAULT_CONFIRMATIONS = 3      # zmod's stall_count
DEFAULT_RUNOUT_CONFIRMATIONS = 1   # zmod's silk_count


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

    `watch_runout` turns on zmod's silk check: the lane's own filament bit
    going FALSE during a move. Both are only tested while the board is in
    `activity`, because outside that state the bits are describing something we
    did not ask for.
    """

    def __init__(self, channel, activity=None, watch_stall=True,
                 watch_runout=False, confirmations=DEFAULT_CONFIRMATIONS,
                 runout_confirmations=DEFAULT_RUNOUT_CONFIRMATIONS):
        if confirmations < 1 or runout_confirmations < 1:
            raise ValueError("confirmations must be at least 1")
        self.channel = channel
        self.activity = activity
        self.watch_stall = watch_stall
        self.watch_runout = watch_runout
        self.confirmations = confirmations
        self.runout_confirmations = runout_confirmations
        self._runout_run = 0
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
            self._runout_run = self._stall_run = 0

        if status.is_ready:
            ## zmod's wait_for_state returns success the moment F13 reports
            ## READY, with no precondition that the activity was ever seen:
            ##     if state == FFS_STATUS_READY: return True, RET_OK, ...
            ## Requiring the activity first meant a transition the poll missed -
            ## it polls every second, zmod every 0.2 - hung until the timeout and
            ## then blamed the board for "never reaching loading" when the move
            ## had actually finished. Reaching the activity is not the success
            ## condition; coming back from it is.
            ##
            ## The race this guarded against - reading READY from before the
            ## command landed - is handled upstream instead: _await only acts on
            ## a status newer than the one it started with.
            return Outcome(FINISHED, status, elapsed)
        return Outcome(WAITING, status, elapsed)

    def _check_sensors(self, status, elapsed):
        ## Silk first, as zmod checks it first: a lane that lost its filament
        ## has also stopped moving, and "there is nothing to feed" is the more
        ## useful of the two answers.
        if self.watch_runout:
            ## zmod's silk={'count': silk_count, 'status': False} -> RET_SILK,
            ## "No filament N in IFS". It is a FAILURE in both directions: on a
            ## load the spool ran out, on an unload the strand left the lane.
            if status.has_filament(self.channel):
                self._runout_run = 0
            else:
                self._runout_run += 1
                if self._runout_run >= self.runout_confirmations:
                    return Outcome(RUNOUT, status, elapsed,
                                   "channel %d has no filament in the IFS"
                                   % self.channel)

        if self.watch_stall:
            ## The board's motion bit is SET while filament moves, so a jam is
            ## its sustained ABSENCE during a move we asked for. Measured with
            ## an empty channel as the control; zmod's wait agrees.
            if status.is_moving(self.channel):
                self._stall_run = 0
            else:
                self._stall_run += 1
                if self._stall_run >= self.confirmations:
                    return Outcome(STALLED, status, elapsed,
                                   "channel %d stopped moving" % self.channel)
        return None

    def timed_out(self, status=None, elapsed=0.0):
        return Outcome(TIMED_OUT, status, elapsed,
                       "board never reached %s"
                       % ifs_status.activity_name(self.activity)
                       if self.activity is not None else "board never finished")


## ---------------------------------------------------------------------------
## Motion parameters
## ---------------------------------------------------------------------------
##
## The choreography of a load - what order, when to heat, where to purge - lives
## in gcode macros, not here. That is deliberate: a macro can be read, run and
## overridden by the user from the console, shows up as a button in Mainsail,
## and composes with PRINT_START and tool changes. What stays in Python is the
## part Jinja cannot do: a command plus the wait that decides whether it worked.
##
## This is the numbers those macros need, taken from the printer's OWN
## `Multicolour` block rather than constants we invented. zmod's "defaults" are
## these values copied out, so this reads the source rather than the copy.


class Parameters(object):
    """Load and unload distances, from the printer's own settings.

    `tube_mm` and `ifs_speed` have no home in `Multicolour`, so they are ours:
    how far a lane is from the toolhead, and how fast the IFS pushes. The speed
    follows stock's observed insert of 1200 mm/min.

    A load does NOT feed `tube_mm`: the board refuses an over-long feed outright
    ("F10 C1 L1000 S1200 refused: FFS not ready."), so `load_empty_mm` and
    `load_full_mm` carry zmod's proven autoinsert distances instead. The feed
    still ends early when the toolhead sensor sees filament, so a part-fed lane
    cannot overshoot; the distance is the board's limit, not the target.
    """

    DEFAULTS = {
        "first_purge_mm": 100.0, "first_purge_speed": 300.0, "first_fan": 0.0,
        "second_purge_mm": 30.0, "second_purge_speed": 300.0,
        "second_fan": 255.0,
        "unload_extruder_mm": 60.0, "unload_ifs_mm": 70.0,
        "unload_speed": 600.0,
        ## How far a load feeds, straight from zmod's defaults
        ## (filament_autoinsert_empty_length / _full_length). The board does NOT
        ## cap a feed at these: "F10 C1 L1000 S1200 refused: FFS not ready." was
        ## a clamp that had not settled yet, and zmod's own load asks for 1000.
        "load_empty_mm": 600.0, "load_full_mm": 550.0,
        ## The shear either side of the cut, from zmod's _CUT_PRUTOK
        ## (FILAMENT_UNLOAD_BEFORE_CUTTING / _AFTER_CUTTING). The AD5X cuts
        ## filament by driving the toolhead into a fixed blade, and the stub
        ## left behind has to come back out of the extruder afterwards.
        "cut_before_mm": 0.0, "cut_after_mm": 5.0,
    }
    ## Multicolour key -> our name. FlashForge spells it "Frist".
    FROM_MULTICOLOUR = {
        "FristESpace": "first_purge_mm", "FristESpeed": "first_purge_speed",
        "FristFanSpeed": "first_fan",
        "SecondESpace": "second_purge_mm", "SecondESpeed": "second_purge_speed",
        "SecondFanSpeed": "second_fan",
        "UnloadESpace": "unload_extruder_mm",
        "UnloadIFSSpace": "unload_ifs_mm", "UnloadSpeed": "unload_speed",
    }

    def __init__(self, tube_mm=1000.0, ifs_speed=1200.0, **overrides):
        self.tube_mm = float(tube_mm)
        self.ifs_speed = float(ifs_speed)
        for name, default in self.DEFAULTS.items():
            setattr(self, name, float(overrides.pop(name, default)))
        if overrides:
            raise TypeError("unknown parameters: %s" % sorted(overrides))

    @classmethod
    def from_multicolour(cls, block, **kwargs):
        """Build from the printer's `Multicolour` section, keys it lacks aside."""
        values = {}
        for key, name in cls.FROM_MULTICOLOUR.items():
            if isinstance(block.get(key), (int, float)):
                values[name] = float(block[key])
        values.update(kwargs)
        return cls(**values)

    def as_dict(self):
        """Everything a macro needs, for get_status()."""
        names = ["tube_mm", "ifs_speed"] + sorted(self.DEFAULTS)
        return {name: getattr(self, name) for name in names}
