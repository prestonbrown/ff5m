## Operations on the AD5X IFS (4-channel filament system) board.
##
## The nine opcodes that do something: feed, retract, clamp, release, mark
## inserted, stop, reset the driver, release everything, and poll. Each one
## validates its arguments, builds the command, and checks the board's reply
## against what the firmware says success looks like.
##
## Sits on IfsLink for the wire and IfsStatus for the F13 line. Knows nothing
## about klippy, so it is exercised off-rig against a scripted board.
##
## Command syntax and the expected replies are in docs/AD5X_IFS_PROTOCOL.md.
## The nine opcodes and their exact argument order match ghzserg's zmod_ifs.py,
## which is the only known working driver.
##
## Copyright (C) 2026, Preston Brown
## Portions derived from zmod (C) 2025-2026 ghzserg <https://github.com/ghzserg/zmod/>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import ifs_status


class IfsOperationError(Exception):
    """The board refused, or answered something we do not recognise.

    `payload` is the board's own words when it had any, so a caller can report
    "FFS not ready." rather than a description of it.
    """

    def __init__(self, message, command=None, payload=None):
        Exception.__init__(self, message)
        self.command = command
        self.payload = payload


class IfsOperations(object):
    """Every command the board acts on, one method each.

    Channel numbers are validated against what F19 reported at connect rather
    than a hardcoded four, so a board that is not four-channel is refused
    locally instead of being asked and told "FFS channel not exist."
    """

    ## F112's reply gained a " yes." suffix in some firmware revision; 3.0.6
    ## does not send it. Accepting both is the documented version spread.
    STOP_REPLIES = ("", "yes.")

    def __init__(self, link, channel_count=None):
        self._link = link
        self._channel_count = channel_count

    @property
    def channel_count(self):
        """Channels this board has, probed if the link managed to ask."""
        if self._channel_count is not None:
            return self._channel_count
        capabilities = getattr(self._link, "capabilities", None)
        if capabilities is not None:
            return capabilities.channel_count
        return ifs_status.MAX_CHANNELS

    ## -- movement -----------------------------------------------------------

    def feed(self, channel, length_mm, speed_mm_min):
        """Push filament from a channel towards the extruder."""
        return self._move("F10", "feeding", channel, length_mm, speed_mm_min)

    def retract(self, channel, length_mm, speed_mm_min):
        """Pull filament back into a channel."""
        return self._move("F11", "exiting", channel, length_mm, speed_mm_min)

    def _move(self, opcode, verb, channel, length_mm, speed_mm_min):
        self._check_channel(channel)
        length_mm = self._positive("length", length_mm)
        speed_mm_min = self._positive("speed", speed_mm_min)
        command = "%s C%d L%d S%d" % (opcode, channel, length_mm, speed_mm_min)
        return self._expect(command,
                            ("FFS channel %d %s." % (channel, verb),))

    def stop(self):
        """Stop whatever the board is feeding."""
        return self._expect("F112", self.STOP_REPLIES)

    ## -- clamping -----------------------------------------------------------

    def clamp(self, channel):
        """Select and clamp a channel."""
        self._check_channel(channel)
        return self._expect("F24 C%d" % channel, ("chan %d." % channel,))

    def release(self, channel):
        """Unclamp one channel."""
        self._check_channel(channel)
        return self._expect("F39 C%d" % channel,
                            ("FFS channel %d release." % channel,))

    def release_all(self):
        """Unclamp every channel."""
        return self._expect("F18", ("",))

    def mark_inserted(self, channel):
        """Tell the board filament has been put into a channel."""
        self._check_channel(channel)
        return self._expect("F23 C%d" % channel, ("chan %d." % channel,))

    ## -- maintenance --------------------------------------------------------

    def reset_driver(self):
        """Reset the board's stepper driver after a driver error.

        The literal "C" is the argument the firmware expects; it is not a
        channel, and no channel number goes here.
        """
        return self._expect("F15 C", ("",))

    def poll_status(self):
        """One F13 reading as an IfsStatus."""
        response = self._link.request("F13")
        if response.is_error:
            raise IfsOperationError("board refused F13: %s" % response.payload,
                                    "F13", response.payload)
        return ifs_status.parse_status(response.payload, self.channel_count)

    def capabilities(self):
        """What the board reported about itself, or None if it never said."""
        return getattr(self._link, "capabilities", None)

    ## -- plumbing -----------------------------------------------------------

    def _check_channel(self, channel):
        count = self.channel_count
        if not isinstance(channel, int) or isinstance(channel, bool):
            raise IfsOperationError("channel must be an integer, got %r"
                                    % (channel,))
        if channel < 1 or channel > count:
            raise IfsOperationError(
                "channel %d is out of range; this board has %d"
                % (channel, count))

    @staticmethod
    def _positive(name, value):
        if value is None or value <= 0:
            raise IfsOperationError("%s must be positive, got %r"
                                    % (name, value))
        return int(value)

    def _expect(self, command, accepted):
        """Send a command and require one of the payloads success looks like.

        The board prefixes failures with "ok." exactly as it prefixes successes
        - "F10 ok. FFS not ready." is a refusal - so the payload is the only
        thing that says whether the command happened.
        """
        response = self._link.request(command)
        if response.payload in accepted:
            return response
        if response.is_error:
            raise IfsOperationError(
                "%s refused: %s" % (command, response.payload),
                command, response.payload)
        raise IfsOperationError(
            "%s answered %r, expected one of %r"
            % (command, response.payload, list(accepted)),
            command, response.payload)
