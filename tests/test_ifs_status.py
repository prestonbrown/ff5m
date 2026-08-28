## Tests for the AD5X IFS status model.
##
## The state values exercised here are the ones ghzserg's zmod_ifs.py enumerates
## in its own comments ("18, 29, 40" and so on), which is the only independent
## record of what the board reports for channels 2-4.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest


import ifs_modules

S = ifs_modules.load("ifs_status")


## A full F13 reply as the firmware formats it: channel 2 loading, filament in
## channels 1 and 3, nothing stalled.
LINE = ("F13 ok. FFS_state: 22 silk_state: 5 chan: 2 ffs_channels_insert: 0 "
        "stall_state: 0 jinsi_GCONF: 000001c4 qiehuan_GCONF: 000000c0 ")


def status(**overrides):
    fields = dict(FFS_state=5, silk_state=0, chan=0,
                  ffs_channels_insert=0, stall_state=0)
    fields.update(overrides)
    text = " ".join("%s: %d" % (k, v) for k, v in fields.items())
    return S.parse_status(text)


class TestStateTable(unittest.TestCase):
    def test_every_activity_and_channel_is_covered(self):
        ## 3 whole-board states + 4 channelled activities across 4 channels.
        self.assertEqual(len(S.STATE_TABLE), 3 + 4 * 4)

    def test_zmods_documented_values(self):
        ## Straight from zmod_ifs.py's own comments. If the stride were wrong
        ## these are the numbers that would disagree.
        cases = {
            S.CLAMPED: (7, 18, 29, 40),
            S.LOADING: (11, 22, 33, 44),
            S.UNCLAMPING: (12, 23, 34, 45),
            S.UNLOADING: (15, 26, 37, 48),
        }
        for activity, values in cases.items():
            for index, value in enumerate(values):
                self.assertEqual(S.decode_state(value),
                                 (activity, index + 1),
                                 "state %d" % value)

    def test_whole_board_states_carry_no_channel(self):
        for value in (S.POLLING, S.READY, S.DRIVER_ERROR):
            self.assertEqual(S.decode_state(value), (value, 0))

    def test_no_two_states_collide(self):
        ## _build_state_table asserts on a collision; widening the channel count
        ## is the change that would introduce one.
        self.assertEqual(len(set(S.STATE_TABLE)), len(S.STATE_TABLE))

    def test_state_value_is_the_inverse(self):
        for value, (activity, channel) in S.STATE_TABLE.items():
            self.assertEqual(S.state_value(activity, channel), value)

    def test_an_unknown_state_is_passed_through_not_swallowed(self):
        ## A newer board may report states this table does not have. Losing the
        ## number would make that undiagnosable.
        self.assertEqual(S.decode_state(99), (99, 0))
        self.assertIn("99", S.activity_name(99))


class TestParsing(unittest.TestCase):
    def test_a_full_line(self):
        result = S.parse_status(LINE)
        self.assertEqual(result.state, 22)
        self.assertEqual(result.activity, S.LOADING)
        self.assertEqual(result.activity_channel, 2)
        self.assertEqual(result.active_channel, 2)
        self.assertEqual(result.loaded_channels, [1, 3])
        self.assertEqual(result.moving_channels, [])
        self.assertEqual(result.feeder_gconf, 0x000001c4)
        self.assertEqual(result.selector_gconf, 0x000000c0)

    def test_the_payload_alone_parses_too(self):
        payload = LINE.split("ok.", 1)[1]
        self.assertEqual(S.parse_status(payload).state, 22)

    def test_a_missing_field_raises(self):
        ## The point of the exercise: zmod's per-field regex leaves a missing
        ## field at 0, so a garbled line reads as a healthy idle board.
        for field in S.REQUIRED_FIELDS:
            broken = " ".join(part for part in LINE.split()
                              if not part.startswith(field + ":"))
            with self.assertRaises(S.IfsStatusError) as caught:
                S.parse_status(broken)
            ## Name the field, so a test that passes for the wrong reason -
            ## "chan" also occurring inside "ffs_channels_insert" is the trap -
            ## fails instead.
            self.assertIn(field, str(caught.exception))

    def test_empty_and_junk_raise(self):
        for text in ("", None, "F13 ok.", "hello world"):
            with self.assertRaises(S.IfsStatusError):
                S.parse_status(text)

    def test_a_garbled_line_is_not_an_idle_board(self):
        with self.assertRaises(S.IfsStatusError):
            S.parse_status("F13 ok. FFS_state: 5 silk_stat")

    def test_absent_gconf_is_none_not_zero(self):
        ## Zero is a legitimate register value; absence must be distinguishable.
        result = status()
        self.assertIsNone(result.feeder_gconf)
        self.assertIsNone(result.selector_gconf)


class TestMasks(unittest.TestCase):
    def test_silk_mask_is_per_channel(self):
        self.assertEqual(status(silk_state=0b1010).loaded_channels, [2, 4])
        self.assertEqual(status(silk_state=0).loaded_channels, [])
        self.assertEqual(status(silk_state=0b1111).loaded_channels,
                         [1, 2, 3, 4])

    def test_has_filament(self):
        result = status(silk_state=0b0101)
        self.assertTrue(result.has_filament(1))
        self.assertFalse(result.has_filament(2))
        self.assertTrue(result.has_filament(3))

    def test_motion_on_a_channel_or_anywhere(self):
        ## The wire field is called stall_state but reports MOTION: measured
        ## with an empty channel as a control, the bit is set while that
        ## channel's filament moves and clear when it does not.
        result = status(stall_state=0b1000)
        self.assertEqual(result.moving_channels, [4])
        self.assertTrue(result.is_moving(4))
        self.assertFalse(result.is_moving(1))
        self.assertTrue(result.is_moving(0))
        self.assertFalse(status(stall_state=0).is_moving(0))

    def test_channel_count_bounds_the_mask(self):
        ## A two-channel board must not report channels 3 and 4 because the
        ## firmware sent a full nibble.
        result = S.IfsStatus(state=5, silk_mask=0b1111, active_channel=0,
                             insert_mask=0, stall_mask=0, channel_count=2)
        self.assertEqual(result.loaded_channels, [1, 2])

    def test_insert_mask_keeps_every_channel(self):
        ## zmod reduces this with int.bit_length(), which returns the highest
        ## set bit: 0b101 becomes 3 and channel 1's insert is lost.
        self.assertEqual(status(ffs_channels_insert=0b101).inserted_channels,
                         [1, 3])


class TestInsertWatcher(unittest.TestCase):
    def setUp(self):
        self.watcher = S.InsertWatcher()

    def test_nothing_inserted_reports_nothing(self):
        self.assertEqual(self.watcher.update(status()), [])

    def test_an_insert_fires_once(self):
        self.assertEqual(
            self.watcher.update(status(ffs_channels_insert=0b1)), [1])
        ## The board reports insert as a level, so the same mask next poll is
        ## the same filament, not a second insertion.
        self.assertEqual(
            self.watcher.update(status(ffs_channels_insert=0b1)), [])

    def test_only_the_newly_inserted_channel_fires(self):
        self.watcher.update(status(ffs_channels_insert=0b1))
        self.assertEqual(
            self.watcher.update(status(ffs_channels_insert=0b101)), [3])

    def test_two_channels_at_once_both_fire(self):
        ## The failure this exists to prevent: collapsing the mask to its
        ## highest bit drops channel 1 here and it never autoloads.
        self.assertEqual(
            self.watcher.update(status(ffs_channels_insert=0b101)), [1, 3])

    def test_removal_then_reinsertion_fires_again(self):
        self.watcher.update(status(ffs_channels_insert=0b10))
        self.watcher.update(status(ffs_channels_insert=0))
        self.assertEqual(
            self.watcher.update(status(ffs_channels_insert=0b10)), [2])

    def test_inserts_while_busy_are_not_acted_on(self):
        loading = status(FFS_state=22, ffs_channels_insert=0b1)
        self.assertEqual(self.watcher.update(loading), [])

    def test_returning_to_ready_does_not_replay_the_insert(self):
        ## Filament that arrived during a load must not fire an autoload the
        ## moment the board goes ready again.
        self.watcher.update(status(FFS_state=22, ffs_channels_insert=0b1))
        self.assertEqual(
            self.watcher.update(status(ffs_channels_insert=0b1)), [])

    def test_reset_forgets_everything(self):
        self.watcher.update(status(ffs_channels_insert=0b11))
        self.watcher.reset()
        self.assertEqual(
            self.watcher.update(status(ffs_channels_insert=0b11)), [1, 2])


class TestReadyAndError(unittest.TestCase):
    def test_ready(self):
        self.assertTrue(status(FFS_state=S.READY).is_ready)
        self.assertFalse(status(FFS_state=22).is_ready)

    def test_driver_error(self):
        result = status(FFS_state=S.DRIVER_ERROR)
        self.assertTrue(result.is_driver_error)
        self.assertEqual(result.activity_name, "driver_error")

    def test_repr_names_the_activity_and_channel(self):
        self.assertIn("loading ch2", repr(S.parse_status(LINE)))


if __name__ == "__main__":
    unittest.main()
