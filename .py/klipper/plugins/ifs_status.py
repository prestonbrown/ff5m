## Status model for the AD5X IFS (4-channel filament system) board.
##
## Turns one F13 status line into a snapshot: what the board is doing, to which
## channel, which channels hold filament, which are moving, and which have just
## had filament pushed in. Pure parsing - no serial, no klippy - so it is
## testable on its own.
##
## The line's shape is in docs/AD5X_IFS_PROTOCOL.md, recovered from the board's
## firmware. Two departures from ghzserg's zmod_ifs.py, both deliberate and both
## covered by tests:
##
##   * A line that does not parse raises. zmod's regex-per-field approach leaves
##     every field at 0 when the line is garbled, which is indistinguishable
##     from a real "idle, nothing loaded, no stalls" reading.
##   * ffs_channels_insert is treated as the bitmask it is. zmod reduces it with
##     int.bit_length(), which returns the highest set bit, so filament inserted
##     into two channels at once reports only the higher one and the lower
##     channel's insert event is lost.
##
## Copyright (C) 2026, Preston Brown
## Portions derived from zmod (C) 2025-2026 ghzserg <https://github.com/ghzserg/zmod/>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import re


## What the board is doing. The wire value is this base plus a per-channel
## stride, so the pair (activity, channel) is encoded in one number.
POLLING = 3
READY = 5
CLAMPED = 7
LOADING = 11
UNCLAMPING = 12
UNLOADING = 15
DRIVER_ERROR = 127

STATE_DELTA = 11

ACTIVITY_NAMES = {
    POLLING: "polling",
    READY: "ready",
    CLAMPED: "clamped",
    LOADING: "loading",
    UNCLAMPING: "unclamping",
    UNLOADING: "unloading",
    DRIVER_ERROR: "driver_error",
}

## Only these four are ever observed carrying a channel offset: zmod's own
## comments enumerate them ("18, 29, 40" for CLAMPED, "22, 33, 44" for LOADING,
## "23, 34, 45" for UNCLAMPING, "26, 37, 48" for UNLOADING) and its operations
## only ever wait on LOADING and UNLOADING with a port. POLLING, READY and
## DRIVER_ERROR are whole-board conditions and are not offset here - inventing
## an offset for them would be claiming knowledge the evidence does not support.
CHANNELED_ACTIVITIES = (CLAMPED, LOADING, UNCLAMPING, UNLOADING)

MAX_CHANNELS = 4


def _build_state_table(max_channels=MAX_CHANNELS):
    """(activity, channel) for every state value the board can report.

    Every entry is distinct - the bases and the stride of 11 never collide
    across the range - so the decode is unambiguous rather than a guess.
    """
    table = {}
    for activity in (POLLING, READY, DRIVER_ERROR):
        table[activity] = (activity, 0)
    for activity in CHANNELED_ACTIVITIES:
        for channel in range(1, max_channels + 1):
            value = activity + (channel - 1) * STATE_DELTA
            if value in table:
                raise AssertionError(
                    "state %d is ambiguous: %r and (%d, %d)"
                    % (value, table[value], activity, channel))
            table[value] = (activity, channel)
    return table


STATE_TABLE = _build_state_table()


def state_value(activity, channel=0):
    """The wire value for an activity on a channel. Inverse of the table."""
    if activity in CHANNELED_ACTIVITIES and channel:
        return activity + (channel - 1) * STATE_DELTA
    return activity


def decode_state(value):
    """(activity, channel) for a state value; (value, 0) when unrecognised.

    An unknown value is passed through rather than rejected: a newer board may
    report states this table does not have, and losing the raw number would
    make that undiagnosable.
    """
    return STATE_TABLE.get(value, (value, 0))


def activity_name(activity):
    return ACTIVITY_NAMES.get(activity, "unknown(%s)" % activity)


def mask_to_channels(mask, channel_count=MAX_CHANNELS):
    """Bit i of the mask is channel i+1."""
    return [i + 1 for i in range(channel_count) if (mask >> i) & 1]


class IfsStatusError(ValueError):
    """The status line could not be understood."""


## Fields the line must carry. A missing one means we are not looking at an F13
## reply, and guessing zeros for it would fabricate a plausible-looking state.
REQUIRED_FIELDS = ("FFS_state", "silk_state", "chan", "ffs_channels_insert",
                   "stall_state")

## Present on 3.0.6, but a different revision may not send them, and they are
## diagnostics rather than state.
OPTIONAL_HEX_FIELDS = {"jinsi_GCONF": "feeder_gconf",
                       "qiehuan_GCONF": "selector_gconf"}


class IfsStatus(object):
    """One F13 reading. Immutable; build a new one per poll."""

    ## The wire field is called `stall_state`, but it reports MOTION: measured
    ## with an empty channel as a control, the bit is SET while that channel's
    ## filament is moving and CLEAR when it is not. zmod's wait agrees - it
    ## declares a jam when the bit goes CLEAR during a commanded move. Naming it
    ## after what it means, not what the firmware calls it, because a method
    ## called is_stalled() that returns true for healthy motion is a trap.
    def __init__(self, state, silk_mask, active_channel, insert_mask,
                 stall_mask, channel_count=MAX_CHANNELS,
                 feeder_gconf=None, selector_gconf=None, raw=""):
        self.state = state
        self.silk_mask = silk_mask
        self.active_channel = active_channel
        self.insert_mask = insert_mask
        self.motion_mask = stall_mask
        self.channel_count = channel_count
        self.feeder_gconf = feeder_gconf
        self.selector_gconf = selector_gconf
        self.raw = raw
        self.activity, self.activity_channel = decode_state(state)

    ## -- derived views ------------------------------------------------------

    @property
    def activity_name(self):
        return activity_name(self.activity)

    @property
    def loaded_channels(self):
        """Channels reporting filament present."""
        return mask_to_channels(self.silk_mask, self.channel_count)

    @property
    def moving_channels(self):
        """Channels whose filament is moving right now."""
        return mask_to_channels(self.motion_mask, self.channel_count)

    @property
    def inserted_channels(self):
        """Every channel with filament pushed in - not just the highest."""
        return mask_to_channels(self.insert_mask, self.channel_count)

    @property
    def is_ready(self):
        return self.activity == READY

    @property
    def is_driver_error(self):
        return self.activity == DRIVER_ERROR

    def has_filament(self, channel):
        return channel in self.loaded_channels

    def is_moving(self, channel=0):
        """Filament moving on one channel, or on any channel when asked for 0.

        A move that was commanded and is NOT moving is the jam - see
        ifs_sequences.StateWaiter, which is where that decision belongs.
        """
        if not channel:
            return self.motion_mask != 0
        return channel in self.moving_channels

    def __repr__(self):
        return ("IfsStatus(%s%s, loaded=%s, moving=%s)"
                % (self.activity_name,
                   " ch%d" % self.activity_channel if self.activity_channel
                   else "",
                   self.loaded_channels, self.moving_channels))


def parse_status(text, channel_count=MAX_CHANNELS):
    """Parse an F13 payload into an IfsStatus.

    `text` is the payload after the "F13 ok." prefix, or the whole line - both
    work. Raises IfsStatusError when a required field is missing, rather than
    returning a zeroed status that reads as a healthy idle board.
    """
    if not text:
        raise IfsStatusError("empty status line")

    values = {}
    for field in REQUIRED_FIELDS:
        match = re.search(r"\b%s:\s*(\d+)" % field, text)
        if match is None:
            raise IfsStatusError("status line has no %s: %r" % (field, text))
        values[field] = int(match.group(1))

    status = IfsStatus(
        state=values["FFS_state"],
        silk_mask=values["silk_state"],
        active_channel=values["chan"],
        insert_mask=values["ffs_channels_insert"],
        stall_mask=values["stall_state"],   # motion, despite the name
        channel_count=channel_count,
        raw=text)

    for field, attribute in OPTIONAL_HEX_FIELDS.items():
        match = re.search(r"\b%s:\s*([0-9a-fA-F]{8})\b" % field, text)
        if match is not None:
            setattr(status, attribute, int(match.group(1), 16))
    return status


class InsertWatcher(object):
    """Edge-detects filament being pushed into a channel.

    The board reports insert as a level, not an event, so an autoload must fire
    on the transition. Per channel, because two channels can gain filament
    between two polls and both events matter - zmod collapses the mask to its
    highest bit and loses the lower one.

    Insertions are only reported while the board is ready; during a load or an
    unload the mask moves for reasons that are not a user pushing filament in.
    """

    def __init__(self):
        self._seen_mask = 0

    def update(self, status):
        """Channels newly inserted since the last call, low channel first."""
        if not status.is_ready:
            ## Not an insert we should act on, but still the current truth -
            ## tracking it here stops the return to ready from replaying every
            ## channel as freshly inserted.
            self._seen_mask = status.insert_mask
            return []
        new_mask = status.insert_mask & ~self._seen_mask
        self._seen_mask = status.insert_mask
        return mask_to_channels(new_mask, status.channel_count)

    def reset(self):
        self._seen_mask = 0
