## Tests for IFS Jacker support.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest

import ifs_modules
import ifs_klipper_fakes as fakes

JACKER = ifs_modules.load("ifs_jacker")
IFS_LINK = ifs_modules.load("ifs_link")
IFS_STATUS = ifs_modules.load("ifs_status")


A_JACKER_REPLY = ('software: "IFS Jacker" version: "3.2.1" '
                  'channel_count: 4 peripheral_count: 2')
AN_OLD_JACKER_REPLY = 'software: "IFS Jacker" version: "2.1.0"'
A_STATUS_WITH_PERIPHERALS = (
    "FFS_state: 5 silk_state: 3 chan: 2 ffs_channels_insert: 0 "
    "stall_state: 0 p0_fan: 2400 p0_mode: auto p1_temp: 45.5 p1_name: desk")


class ParseProbeTest(unittest.TestCase):
    def test_a_jacker_reply_parses(self):
        result = JACKER.parse_probe(A_JACKER_REPLY)
        self.assertIsNotNone(result)
        self.assertTrue(result.present)
        self.assertEqual(result.version, 3.2)
        self.assertEqual(result.channel_count, 4)
        self.assertEqual(result.peripheral_count, 2)

    def test_an_old_jacker_has_no_peripheral_count(self):
        result = JACKER.parse_probe(AN_OLD_JACKER_REPLY)
        self.assertTrue(result.present)
        self.assertEqual(result.version, 2.1)
        self.assertIsNone(result.peripheral_count)

    def test_anything_else_is_not_a_jacker(self):
        self.assertIsNone(JACKER.parse_probe("FFS not ready."))
        self.assertIsNone(JACKER.parse_probe(""))
        self.assertIsNone(JACKER.parse_probe(None))


class ParsePeripheralsTest(unittest.TestCase):
    def test_tuples_parse_with_coerced_values(self):
        found = JACKER.parse_peripherals(A_STATUS_WITH_PERIPHERALS)
        self.assertEqual(found["0"], {"fan": 2400, "mode": "auto"})
        self.assertEqual(found["1"], {"temp": 45.5, "name": "desk"})

    def test_a_plain_status_line_has_no_peripherals(self):
        plain = ("FFS_state: 5 silk_state: 3 chan: 2 ffs_channels_insert: 0 "
                 "stall_state: 0")
        self.assertEqual(JACKER.parse_peripherals(plain), {})

    def test_the_board_tolerates_jacker_augmented_lines(self):
        """A Jacker appends its tuples to the board's own F13 payload.

        The status parser must keep reading the board's fields out of such a
        line: this is the machine state whenever the device is present, so a
        parser that rejects unknown fields would report a healthy board as
        garbled - a disconnect, on every poll, forever.
        """
        status = IFS_STATUS.parse_status(A_STATUS_WITH_PERIPHERALS)
        self.assertEqual(status.state, 5)
        self.assertEqual(status.silk_mask, 3)
        self.assertEqual(status.active_channel, 2)

    def test_empty_input_is_safe(self):
        self.assertEqual(JACKER.parse_peripherals(None), {})
        self.assertEqual(JACKER.parse_peripherals(""), {})


class FakeIfs(object):
    """Stands in for [ifs]: records commands, answers from a script."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.sent = []
        self.listeners = []

    def execute(self, command):
        self.sent.append(command)
        if not self.replies:
            raise RuntimeError("silent")
        reply = self.replies.pop(0)
        if reply is None:
            raise RuntimeError("silent")
        return IFS_LINK.IfsResponse("Z2", reply)

    def add_status_listener(self, listener):
        self.listeners.append(listener)


class IfsJackerObjectTest(unittest.TestCase):
    def build(self, replies=None):
        printer = fakes.FakePrinter()
        config = fakes.FakeConfig("ifs_jacker", {}, printer)
        obj = JACKER.IfsJacker(config)
        return obj, printer

    def ready(self, obj, printer, fake_ifs):
        printer.add_object("ifs", fake_ifs)
        printer.fire("klippy:ready")

    def test_requires_ifs(self):
        obj, printer = self.build()
        with self.assertRaises(Exception):
            printer.fire("klippy:ready")

    def test_a_silent_probe_chases_with_f13_and_stays_unknown(self):
        """The bare-board case: silence, then prove the link alive.

        The chaser exists because the board holds a command it cannot answer:
        without the F13 the link reads as wedged, which is how a plain IFS
        with a Jacker section gets diagnosed as disconnecting.
        """
        obj, printer = self.build()
        fake = FakeIfs(replies=[None, "ok"])
        self.ready(obj, printer, fake)
        obj.probe()
        self.assertEqual(fake.sent, ["Z2", "F13"])
        self.assertIsNone(obj.present)

    def test_a_jacker_reply_sets_state(self):
        obj, printer = self.build()
        fake = FakeIfs(replies=[A_JACKER_REPLY])
        self.ready(obj, printer, fake)
        result = obj.probe()
        self.assertTrue(result.present)
        self.assertTrue(obj.present)
        self.assertEqual(obj.version, 3.2)
        self.assertEqual(obj.peripherals, {"0": {}, "1": {}})

    def test_peripherals_track_the_status_stream(self):
        obj, printer = self.build()
        fake = FakeIfs(replies=[A_JACKER_REPLY])
        self.ready(obj, printer, fake)
        obj.probe()
        status = IFS_STATUS.parse_status(A_STATUS_WITH_PERIPHERALS)
        fake.listeners[0](status)
        self.assertEqual(obj.peripherals["0"]["fan"], 2400)
        self.assertEqual(obj.peripherals["1"]["temp"], 45.5)
        self.assertEqual(obj.get_status()["peripherals"]["1"]["name"], "desk")

    def test_old_firmware_records_no_peripherals(self):
        obj, printer = self.build()
        fake = FakeIfs(replies=[AN_OLD_JACKER_REPLY])
        self.ready(obj, printer, fake)
        obj.probe()
        status = IFS_STATUS.parse_status(A_STATUS_WITH_PERIPHERALS)
        fake.listeners[0](status)
        self.assertEqual(obj.peripherals, {})

    def test_status_publishes_detection(self):
        obj, printer = self.build()
        fake = FakeIfs(replies=[A_JACKER_REPLY])
        self.ready(obj, printer, fake)
        obj.probe()
        published = obj.get_status()
        self.assertTrue(published["detected"])
        self.assertEqual(published["version"], 3.2)
        self.assertEqual(published["channel_count"], 4)
        self.assertEqual(published["peripheral_count"], 2)


if __name__ == "__main__":
    unittest.main()
