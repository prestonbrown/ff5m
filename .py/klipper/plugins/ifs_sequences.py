## Waiting for the IFS board to finish, or to tell us why it will not.
##
## The board answers a motion command immediately and then works; what actually
## happened only shows up in the F13 stream afterwards. So every step of a load
## or an unload is "send it, then watch until one of a handful of things
## becomes true", and this is that watching, with no klipper and no serial in
## it so the decisions can be tested on their own.
##
## A sensor condition is only meaningful while the board is in the
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
NOT_REACHED = "not_reached"    # the move ended without the sensor it was aimed at
DRIVER_ERROR = "driver_error"  # the board wants F15 before anything else
TIMED_OUT = "timed_out"

## Outcomes that mean "stop and deal with it", as opposed to a normal finish.
##
## NOT_REACHED is deliberately NOT one of them. A checked feed that simply
## completes the move is allowed to carry straight on to the co-push, where
## the EXTRUDER gear pulls the filament the last stretch in. Failing there instead stopped a load whose filament was
## sitting at the toolhead entry waiting for exactly that - "lane 1 is stuck at
## the toolhead entry, the filament isn't gripped by the gears yet". It is
## still worth reporting, because a feed that ended on the sensor and one that
## merely ran out of length are different things.
PROBLEMS = frozenset([STALLED, RUNOUT, DRIVER_ERROR, TIMED_OUT])

## Consecutive matching polls before a condition counts. Two separate counts:
## a stall has to persist (the motion bit toggles, so single samples read as
## stopped all the time), while the lane's own filament bit is steady and one
## reading of it is enough.
DEFAULT_CONFIRMATIONS = 3
DEFAULT_RUNOUT_CONFIRMATIONS = 1


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

    `watch_runout` turns on the silk check: the lane's own filament bit
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
        self._seen_motion = False

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
            ## Success the moment F13 reports READY, with no precondition that
            ## the activity was ever seen. Requiring the activity first meant a
            ## transition the poll missed - at the background's one-second
            ## cadence - hung until the timeout and then blamed the board for "never reaching loading" when the move
            ## had actually finished. Reaching the activity is not the success
            ## condition; coming back from it is.
            ##
            ## The race this guarded against - reading READY from before the
            ## command landed - is handled upstream instead: _await only acts on
            ## a status newer than the one it started with.
            return Outcome(FINISHED, status, elapsed)
        return Outcome(WAITING, status, elapsed)

    def _check_sensors(self, status, elapsed):
        ## Silk first: a lane that lost its filament has also stopped moving,
        ## and "there is nothing to feed" is the more useful of the two
        ## answers.
        if self.watch_runout:
            ## It is a FAILURE in both directions: on a load the spool ran
            ## out, on an unload the strand left the lane.
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
            ## an empty channel as the control.
            if status.is_moving(self.channel):
                self._seen_motion = True
                self._stall_run = 0
            else:
                self._stall_run += 1
                if self._stall_run >= self.confirmations:
                    ## Whether the lane EVER moved is the first question anyone
                    ## asks of a stall, and it points at different hardware: a
                    ## lane that never started is bound at the IFS itself, one
                    ## that stopped part-way hit something ahead of it. Working
                    ## that out cost a night of reading the motion bit by hand.
                    return Outcome(STALLED, status, elapsed,
                                   "channel %d %s" % (
                                       self.channel,
                                       "stopped moving after %.1fs" % elapsed
                                       if self._seen_motion
                                       else "never started moving"))
        return None

    def timed_out(self, status=None, elapsed=0.0):
        ## What ran out is the wait for the board to come back to READY - not
        ## for it to reach the activity, which is only the window conditions are
        ## judged in. Saying "never reached loading" sent one investigation
        ## after a transition that had happened and been polled straight past.
        if self.activity is None:
            return Outcome(TIMED_OUT, status, elapsed,
                           "the board never came back to ready")
        return Outcome(TIMED_OUT, status, elapsed,
                       "the board never came back to ready from %s"
                       % ifs_status.activity_name(self.activity))


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
## `Multicolour` block rather than constants we invented.


class Parameters(object):
    """Load and unload distances, from the printer's own settings.

    `tube_mm` and `ifs_speed` have no home in `Multicolour`, so they are ours:
    how far a lane is from the toolhead, and how fast the IFS pushes. The speed
    follows stock's observed insert of 1200 mm/min.

    A load does NOT feed `tube_mm`: the board refuses an over-long feed outright
    ("F10 C1 L1000 S1200 refused: FFS not ready."), so `load_empty_mm` and
    `load_full_mm` carry the autoinsert distances instead. The feed
    still ends early when the toolhead sensor sees filament, so a part-fed lane
    cannot overshoot; the distance is the board's limit, not the target.
    """

    DEFAULTS = {
        "first_purge_mm": 100.0, "first_purge_speed": 300.0, "first_fan": 0.0,
        "second_purge_mm": 30.0, "second_purge_speed": 300.0,
        "second_fan": 255.0,
        "unload_extruder_mm": 60.0, "unload_ifs_mm": 70.0,
        "unload_speed": 600.0,
        ## How far a load feeds (the autoinsert empty and full lengths). The
        ## board does NOT cap a feed at these: "F10 C1 L1000 S1200 refused:
        ## FFS not ready." was a clamp that had not settled yet, and a working
        ## load feeds a full 1000.
        "load_empty_mm": 600.0, "load_full_mm": 550.0,
        ## The tube transit is split in two. The feeder holds 3600 mm/min -
        ## measured on hardware at 1200, 2400 and 3600 with a lane watched by
        ## eye, and stopped there because the motor is audibly unhappy long
        ## before anything skips. But the load ENDS by driving into the
        ## extruder gear, and arriving at 60 mm/s rather than 20 is three times
        ## the impact, so only the bulk runs fast: `approach_mm` is the last
        ## stretch, taken at `ifs_speed`. Setting ifs_fast_speed to ifs_speed
        ## restores the single-speed behaviour.
        ##
        ## The toolhead sensor cannot police this either. It is declared to
        ## klipper as a thermistor, and klipper reports those every 0.300 s
        ## (adc_temperature.REPORT_TIME), so the trigger can be up to 18 mm
        ## late at 3600 however fast this module polls it.
        ##
        ## approach_mm is sized by WHERE LANES PARK, not by how close the gear
        ## is. A lane sits 90 mm back after an autoinsert (autoinsert_ret_mm)
        ## and 300 mm back after being moved out of the shared path
        ## (hub_clear_mm), and those are the ordinary tool-change cases. If the
        ## fast phase were longer than that it would be the phase that arrives
        ## at the gear on every normal change - three times harder than before,
        ## with the slow approach never running at all. At 400 the fast phase
        ## only covers tube a parked lane cannot already be sitting in, so it
        ## helps the fully-ejected load and leaves every short one exactly as
        ## it was.
        "ifs_fast_speed": 3600.0, "approach_mm": 400.0,
        ## How far a freshly threaded lane backs off once its tip reaches the
        ## toolhead sensor, so it rests below the extruder gear rather than in
        ## it.
        "autoinsert_ret_mm": 90.0,
        ## How far a threaded lane retreats to give up the shared path at the
        ## hub. Only one lane fits there, so a load of a different lane has to
        ## move the incumbent out of the way first. Measured: 200 mm was enough
        ## to let a blocked lane through, and this leaves margin. It must stay
        ## well under load_empty_mm, or the retreat pushes the lane back out of
        ## the IFS entirely.
        "hub_clear_mm": 300.0,
        ## Extra purge when the incoming material is a DIFFERENT TYPE from the
        ## one coming out, on top of each pass's normal length - both passes
        ## get it.
        ## A colour change flushes in the volume stock allows for; a material
        ## change also has to flush a melt that behaves differently, and PETG
        ## ghosting through the next PLA is the failure it exists to stop.
        ## It is a flat addition, not a matrix: the type strings are compared
        ## and nothing more, so PLA -> PLA-CF costs the same as PLA -> ABS.
        "purge_extra_mm": 90.0,
        ## The shear either side of the cut. The AD5X cuts
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
