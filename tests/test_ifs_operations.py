## Tests for the AD5X IFS operations layer.
##
## The command syntax and expected replies asserted here match ghzserg's
## zmod_ifs.py call sites exactly - that driver is the only evidence of what
## the board accepts, and diverging from it silently would be the expensive
## kind of mistake.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest

import ifs_modules


STATUS = ifs_modules.load("ifs_status")
LINK = ifs_modules.load("ifs_link")
OPS = ifs_modules.load("ifs_operations")


STATUS_LINE = ("FFS_state: 5 silk_state: 3 chan: 0 ffs_channels_insert: 0 "
               "stall_state: 0")


class FakeResponse(object):
    def __init__(self, payload):
        self.payload = payload
        self.opcode = 0
        self.extra = []

    @property
    def is_error(self):
        return self.payload in LINK.ERROR_PAYLOADS


class FakeCapabilities(object):
    def __init__(self, channel_count):
        self.channel_count = channel_count


class FakeLink(object):
    """Answers whatever the script says, and remembers what it was asked."""

    def __init__(self, script=None, channel_count=4):
        self.script = dict(script or {})
        self.sent = []
        self.capabilities = (FakeCapabilities(channel_count)
                             if channel_count else None)
        self.default = ""

    def request(self, command):
        self.sent.append(command)
        return FakeResponse(self.script.get(command, self.default))


def ops(script=None, channel_count=4):
    link = FakeLink(script, channel_count)
    return OPS.IfsOperations(link), link


class TestCommandSyntax(unittest.TestCase):
    """Byte-for-byte the commands zmod_ifs.py sends."""

    def test_feed(self):
        op, link = ops({"F10 C2 L100 S1200": "FFS channel 2 feeding."})
        op.feed(2, 100, 1200)
        self.assertEqual(link.sent, ["F10 C2 L100 S1200"])

    def test_retract(self):
        op, link = ops({"F11 C3 L70 S600": "FFS channel 3 exiting."})
        op.retract(3, 70, 600)
        self.assertEqual(link.sent, ["F11 C3 L70 S600"])

    def test_clamp(self):
        op, link = ops({"F24 C1": "chan 1."})
        op.clamp(1)
        self.assertEqual(link.sent, ["F24 C1"])

    def test_release(self):
        op, link = ops({"F39 C4": "FFS channel 4 release."})
        op.release(4)
        self.assertEqual(link.sent, ["F39 C4"])

    def test_mark_inserted(self):
        op, link = ops({"F23 C2": "chan 2."})
        op.mark_inserted(2)
        self.assertEqual(link.sent, ["F23 C2"])

    def test_release_all(self):
        op, link = ops({"F18": ""})
        op.release_all()
        self.assertEqual(link.sent, ["F18"])

    def test_stop(self):
        op, link = ops({"F112": ""})
        op.stop()
        self.assertEqual(link.sent, ["F112"])

    def test_reset_driver_sends_the_literal_C(self):
        ## "C" here is not a channel. Sending "F15 C1" would be a different
        ## command, and the firmware would not recognise it.
        op, link = ops({"F15 C": ""})
        op.reset_driver()
        self.assertEqual(link.sent, ["F15 C"])

    def test_reinit_drivers(self):
        op, link = ops({"F43": ""})
        op.reinit_drivers()
        self.assertEqual(link.sent, ["F43"])

    def test_jog_selector(self):
        ## D is an absolute position in the same step coordinates the board's
        ## own moves use, so D4096 lands where F24 C2 puts the turret.
        op, link = ops({"F30 D4096": ""})
        op.jog_selector(4096)
        self.assertEqual(link.sent, ["F30 D4096"])

    def test_lengths_are_sent_as_integers(self):
        op, link = ops({"F10 C1 L70 S600": "FFS channel 1 feeding."})
        op.feed(1, 70.4, 600.0)
        self.assertEqual(link.sent, ["F10 C1 L70 S600"])


class TestSelectorJogValidation(unittest.TestCase):
    """F30 takes a raw step count and the board does not sanity-check it.

    The firmware compares the position counter to the target for equality and
    steps toward it, so a target it cannot reach is a motor that runs until
    something stops it. Every bound is checked here rather than there.
    """

    def test_the_slot_ladder_is_accepted(self):
        for position in (0, 2048, 4096, 6144, 8192, 10240, 12288, 14336):
            op, link = ops({"F30 D%d" % position: ""})
            op.jog_selector(position)
            self.assertEqual(link.sent, ["F30 D%d" % position])

    def test_a_full_revolution_is_accepted(self):
        ## 16384 is one turn from zero, which is what the timing probe sends.
        op, link = ops({"F30 D16384": ""})
        op.jog_selector(16384)
        self.assertEqual(link.sent, ["F30 D16384"])

    def test_out_of_range_positions_are_refused_locally(self):
        for position in (-1, -2048, 16385, 100000):
            op, link = ops()
            with self.assertRaises(OPS.IfsOperationError):
                op.jog_selector(position)
            self.assertEqual(link.sent, [])

    def test_a_non_integer_position_is_refused(self):
        for position in (None, "4096", 4096.5, True):
            op, link = ops()
            with self.assertRaises(OPS.IfsOperationError):
                op.jog_selector(position)
            self.assertEqual(link.sent, [])


class TestSelectorGeometry(unittest.TestCase):
    """The turret constants, which the ladder above is derived from."""

    def test_eight_stops_make_one_revolution(self):
        self.assertEqual(OPS.SELECTOR_STEPS_PER_TURN,
                         8 * OPS.SELECTOR_SLOT_PITCH)

    def test_clamp_positions_are_two_slots_apart(self):
        ## Each lane has a clamp stop and a release stop one pitch further on,
        ## so consecutive lanes' clamp positions are two pitches apart.
        self.assertEqual(OPS.SELECTOR_SLOT_PITCH * 2, 4096)


class TestChannelValidation(unittest.TestCase):
    def test_channel_zero_and_above_range(self):
        op, link = ops()
        for channel in (0, 5, -1):
            with self.assertRaises(OPS.IfsOperationError):
                op.clamp(channel)
        ## Rejected locally: nothing reached the board.
        self.assertEqual(link.sent, [])

    def test_a_two_channel_board_rejects_channel_three(self):
        ## The reason F19 is probed at all. zmod hardcodes four.
        op, link = ops(channel_count=2)
        with self.assertRaises(OPS.IfsOperationError):
            op.feed(3, 100, 1200)
        self.assertEqual(link.sent, [])

    def test_non_integers_are_rejected(self):
        op, _ = ops()
        for channel in ("2", 2.0, None, True):
            with self.assertRaises(OPS.IfsOperationError):
                op.clamp(channel)

    def test_every_channel_command_validates(self):
        op, _ = ops()
        for call in (op.clamp, op.release, op.mark_inserted):
            with self.assertRaises(OPS.IfsOperationError):
                call(9)
        for call in (op.feed, op.retract):
            with self.assertRaises(OPS.IfsOperationError):
                call(9, 100, 1200)

    def test_default_channel_count_when_the_link_never_probed(self):
        op, _ = ops(channel_count=None)
        self.assertEqual(op.channel_count, STATUS.MAX_CHANNELS)


class TestMoveValidation(unittest.TestCase):
    def test_length_and_speed_must_be_positive(self):
        op, link = ops()
        for length, speed in ((0, 1200), (-5, 1200), (100, 0), (100, -1),
                              (None, 1200), (100, None)):
            with self.assertRaises(OPS.IfsOperationError):
                op.feed(1, length, speed)
        self.assertEqual(link.sent, [])


class TestReplyHandling(unittest.TestCase):
    def test_a_refusal_carries_the_boards_own_words(self):
        op, _ = ops({"F10 C1 L100 S1200": "FFS not ready."})
        with self.assertRaises(OPS.IfsOperationError) as caught:
            op.feed(1, 100, 1200)
        self.assertEqual(caught.exception.payload, "FFS not ready.")
        self.assertIn("FFS not ready.", str(caught.exception))

    def test_no_channel_selected_is_a_refusal(self):
        op, _ = ops({"F11 C1 L10 S600": "No channel selected."})
        with self.assertRaises(OPS.IfsOperationError):
            op.retract(1, 10, 600)

    def test_a_reply_for_a_different_channel_is_not_success(self):
        ## The board acknowledging channel 3 when we asked for 2 means
        ## something is wrong, not that the clamp succeeded.
        op, _ = ops({"F24 C2": "chan 3."})
        with self.assertRaises(OPS.IfsOperationError):
            op.clamp(2)

    def test_an_unrecognised_reply_is_not_silently_accepted(self):
        op, _ = ops({"F18": "something new"})
        with self.assertRaises(OPS.IfsOperationError):
            op.release_all()

    def test_a_refused_reinit_is_not_silently_accepted(self):
        op, _ = ops({"F43": "FFS not ready."})
        with self.assertRaises(OPS.IfsOperationError):
            op.reinit_drivers()

    def test_a_refused_jog_is_not_silently_accepted(self):
        op, _ = ops({"F30 D4096": "FFS not ready."})
        with self.assertRaises(OPS.IfsOperationError):
            op.jog_selector(4096)

    def test_success_returns_the_response(self):
        op, _ = ops({"F24 C2": "chan 2."})
        self.assertEqual(op.clamp(2).payload, "chan 2.")


class TestStopVersionSpread(unittest.TestCase):
    def test_both_documented_f112_replies_are_accepted(self):
        ## 3.0.6 answers "F112 ok."; some revision answers "F112 ok. yes.".
        for payload in ("", "yes."):
            op, _ = ops({"F112": payload})
            op.stop()

    def test_an_unknown_f112_reply_still_fails(self):
        op, _ = ops({"F112": "no."})
        with self.assertRaises(OPS.IfsOperationError):
            op.stop()


class TestPollStatus(unittest.TestCase):
    def test_returns_a_parsed_status(self):
        op, _ = ops({"F13": STATUS_LINE})
        result = op.poll_status()
        self.assertTrue(result.is_ready)
        self.assertEqual(result.loaded_channels, [1, 2])

    def test_channel_count_reaches_the_parser(self):
        op, _ = ops({"F13": "FFS_state: 5 silk_state: 15 chan: 0 "
                            "ffs_channels_insert: 0 stall_state: 0"},
                    channel_count=2)
        self.assertEqual(op.poll_status().loaded_channels, [1, 2])

    def test_a_refused_poll_raises(self):
        op, _ = ops({"F13": "FFS not ready."})
        with self.assertRaises(OPS.IfsOperationError):
            op.poll_status()

    def test_a_garbled_poll_raises_rather_than_reading_as_idle(self):
        op, _ = ops({"F13": "FFS_state: 5 silk_stat"})
        with self.assertRaises(STATUS.IfsStatusError):
            op.poll_status()


if __name__ == "__main__":
    unittest.main()
