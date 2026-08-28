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
## Load and unload, as plans
## ---------------------------------------------------------------------------
##
## A sequence is a list of steps and nothing else - no serial, no gcode, no
## klipper - so what a load actually does is readable in one place and testable
## without a printer. The executor above klipper turns each step into an IFS
## command or an extruder move.
##
## Distances and speeds come from the printer's OWN `Multicolour` block (see
## flashforge_config), not from constants we invented. zmod's "defaults" are
## those same numbers copied out, so this reads the source rather than the copy.

## What ends a step, when the board's own state is not the answer.
UNTIL_TOOLHEAD_FILAMENT = "toolhead_filament"   # the toolhead sensor sees it
UNTIL_TOOLHEAD_CLEAR = "toolhead_clear"         # and stops seeing it

CLAMP = "clamp"
RELEASE = "release"
FEED = "feed"              # IFS pushes filament towards the toolhead
RETRACT = "retract"        # IFS pulls filament back into its lane
STOP = "stop"              # F112
EXTRUDE = "extrude"        # the printer's own extruder moves
FAN = "fan"
HEAT = "heat"


class Step(object):
    """One action, plus what finishing it looks like."""

    __slots__ = ("kind", "channel", "distance", "speed", "value", "expect",
                 "until", "note")

    def __init__(self, kind, channel=None, distance=None, speed=None,
                 value=None, expect=None, until=None, note=None):
        self.kind = kind
        self.channel = channel
        self.distance = distance
        self.speed = speed
        self.value = value
        self.expect = expect
        self.until = until
        self.note = note

    def __repr__(self):
        bits = [self.kind]
        if self.channel is not None:
            bits.append("ch%d" % self.channel)
        if self.distance is not None:
            bits.append("%gmm" % self.distance)
        if self.speed is not None:
            bits.append("@%g" % self.speed)
        if self.value is not None:
            bits.append("=%g" % self.value)
        return "Step(%s)" % " ".join(bits)


class Parameters(object):
    """Load and unload distances, taken from the printer's own settings.

    `tube_mm` and `ifs_speed` have no home in `Multicolour`, so they are ours:
    an upper bound on how far the lane is from the toolhead, and how fast the
    IFS pushes. They follow zmod's tube length and stock's observed insert,
    which used 1200 mm/min. `tube_mm` is a bound rather than a target - the
    feed ends when the toolhead sensor sees filament.
    """

    DEFAULTS = {
        "first_purge_mm": 100.0, "first_purge_speed": 300.0, "first_fan": 0.0,
        "second_purge_mm": 30.0, "second_purge_speed": 300.0,
        "second_fan": 255.0,
        "unload_extruder_mm": 60.0, "unload_ifs_mm": 70.0,
        "unload_speed": 600.0,
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
        self.tube_mm = tube_mm
        self.ifs_speed = ifs_speed
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


def load_plan(channel, params, temperature=None):
    """Get filament from a lane into the nozzle, purged and ready.

    The IFS pushes the filament the whole way by itself - the extruder motor
    does not run until the purge. What ends the feed is the TOOLHEAD sensor
    seeing filament, not a distance: the tube length is only an upper bound, so
    a lane that is already part-fed does not overshoot.

    This mirrors zmod's _INSERT_PRUTOK_IFS, which is the only known working
    sequence for this board: clamp, then one F10 for the whole tube with its
    wait watching the extruder sensor, then purge. An earlier draft of this had
    the extruder pulling the filament past its own gear mid-feed; that was
    invented, and no driver does it.
    """
    steps = []
    if temperature is not None:
        ## The printer sets min_extrude_temp to 0, so klipper will NOT refuse a
        ## cold extrude. Nothing below us enforces this.
        steps.append(Step(HEAT, value=temperature,
                          note="klipper will not refuse a cold extrude here"))
    steps.append(Step(CLAMP, channel=channel, expect=ifs_status.CLAMPED))
    steps.append(Step(FEED, channel=channel, distance=params.tube_mm,
                      speed=params.ifs_speed, expect=ifs_status.LOADING,
                      until=UNTIL_TOOLHEAD_FILAMENT,
                      note="tube length is a bound; the toolhead sensor ends it"))
    steps.append(Step(STOP))
    steps.append(Step(FAN, value=params.first_fan))
    steps.append(Step(EXTRUDE, distance=params.first_purge_mm,
                      speed=params.first_purge_speed, note="purge"))
    steps.append(Step(FAN, value=params.second_fan))
    steps.append(Step(EXTRUDE, distance=params.second_purge_mm,
                      speed=params.second_purge_speed, note="purge, cooling"))
    steps.append(Step(FAN, value=0.0))
    steps.append(Step(RELEASE, channel=channel))
    return steps


def unload_plan(channel, params, temperature=None):
    """Take filament out of the nozzle and back into its lane."""
    steps = []
    if temperature is not None:
        steps.append(Step(HEAT, value=temperature,
                          note="molten before pulling, or it snaps"))
    steps.append(Step(CLAMP, channel=channel, expect=ifs_status.CLAMPED))
    steps.append(Step(EXTRUDE, distance=-params.unload_extruder_mm,
                      speed=params.unload_speed,
                      note="the extruder pulls it back off the sensor first"))
    steps.append(Step(RETRACT, channel=channel, distance=params.unload_ifs_mm,
                      speed=params.unload_speed, expect=ifs_status.UNLOADING,
                      until=UNTIL_TOOLHEAD_CLEAR,
                      note="then the IFS takes it the rest of the way"))
    steps.append(Step(STOP))
    steps.append(Step(RELEASE, channel=channel))
    return steps
