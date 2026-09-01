## Tests for the AD5X IFS diagnostics.
##
## Every response string replayed here was read off a live board at firmware
## 3.0.6 - see docs/AD5X_IFS_PROTOCOL.md.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest


import ifs_modules


D = ifs_modules.load("ifs_diagnostics")

## Verbatim from the board.
LIVE = {
    "F14": "stall: 0 0 0 0",
    "F40": "stall count: C1: 1 C2: 462 C3: 1 C4: 1",
    "F42": "stepper_motor: 0 stepper_motor_irun: 0",
    "F50": "GCONF: 000001dc", "F51": "GSTAT: 00000001",
    "F52": "CHOPCONF: 00000000", "F53": "DRV_STATUS: 80000000",
    "F54": "PWMCONF: 00000000",
    "F60": "GCONF: 000001dc", "F61": "GSTAT: 00000000",
    "F62": "CHOPCONF: 00000000", "F63": "DRV_STATUS: 00000000",
    "F64": "PWMCONF: 00000000",
}
F21_EXTRA = ["silk: 199 333 1688 271", "stall: 2048 2128 3417 2146"]


class FakeResponse:
    def __init__(self, payload, extra=None):
        self.payload = payload
        self.extra = extra or []


class FakeCaps:
    version = "3.0.6"
    channel_count = 4


class FakeLink:
    def __init__(self, script=None, fail=(), drain_error=None):
        self.script = dict(script or LIVE)
        self.fail = set(fail)
        self.capabilities = FakeCaps()
        self.asked = []
        self.drains = 0
        self.drain_error = drain_error

    def drain_pending(self):
        self.drains += 1
        if self.drain_error:
            raise RuntimeError(self.drain_error)
        return 0

    def request(self, command):
        self.asked.append(command)
        if command in self.fail:
            raise RuntimeError("board silent on %s" % command)
        if command == "F21":
            return FakeResponse("", list(F21_EXTRA))
        return FakeResponse(self.script.get(command, ""))


class TestRegisterDecoding(unittest.TestCase):
    def test_gstat_reset_is_what_the_board_reports(self):
        self.assertEqual(D.decode_gstat(0x00000001), ["reset"])
        self.assertEqual(D.decode_gstat(0x00000000), [])

    def test_gstat_driver_error(self):
        self.assertIn("driver_error", D.decode_gstat(0x2))
        self.assertIn("undervoltage_charge_pump", D.decode_gstat(0x4))

    def test_drv_status_standstill_is_universal(self):
        flags, family = D.decode_drv_status(0x80000000)
        self.assertEqual(flags, ["standstill"])
        self.assertEqual(family, "tmc2209")

    def test_drv_status_real_faults(self):
        flags, _ = D.decode_drv_status(0x00000042)
        self.assertIn("overtemp", flags)
        self.assertIn("open_load_a", flags)

    def test_an_unknown_family_still_says_something_is_wrong(self):
        ## Refusing to decode is fine; staying silent about a set fault bit is
        ## not. bit 31 is universal, so it must not read as a fault.
        flags, family = D.decode_drv_status(0x80000040, family="mystery")
        self.assertIsNone(family)
        self.assertIn("standstill", flags)
        self.assertIn("unknown_fault_bits", flags)
        clean, _ = D.decode_drv_status(0x80000000, family="mystery")
        self.assertNotIn("unknown_fault_bits", clean)

    def test_every_flag_has_a_description(self):
        for table in (D.GSTAT_FLAGS, D.DRV_STATUS_UNIVERSAL,
                      D.TMC2209_DRV_STATUS):
            for _, name, _ in table:
                self.assertNotEqual(D.describe(name), name, name)


class TestParsers(unittest.TestCase):
    def test_register(self):
        self.assertEqual(D.parse_register("GCONF: 000001dc"), 0x1dc)
        self.assertEqual(D.parse_register("DRV_STATUS: 80000000"), 0x80000000)
        self.assertIsNone(D.parse_register("GCONF: nope"))
        self.assertIsNone(D.parse_register(""))

    def test_stall_counts(self):
        self.assertEqual(D.parse_stall_counts(LIVE["F40"]), [1, 462, 1, 1])
        self.assertIsNone(D.parse_stall_counts("nothing here"))

    def test_quad_picks_the_right_label(self):
        joined = " ".join(F21_EXTRA)
        self.assertEqual(D.parse_quad(joined, "silk"), [199, 333, 1688, 271])
        self.assertEqual(D.parse_quad(joined, "stall"), [2048, 2128, 3417, 2146])
        self.assertIsNone(D.parse_quad(joined, "absent"))

    def test_stepper(self):
        self.assertEqual(D.parse_stepper(LIVE["F42"]), (0, 0))
        self.assertIsNone(D.parse_stepper("stepper_motor: 3"))


class TestReadDiagnostics(unittest.TestCase):
    def test_the_live_board_reading(self):
        diag = D.read_diagnostics(FakeLink())
        self.assertEqual(diag.version, "3.0.6")
        self.assertEqual(diag.channel_count, 4)
        self.assertEqual(diag.stall_counts, [1, 462, 1, 1])
        self.assertEqual(diag.silk_raw, [199, 333, 1688, 271])
        self.assertEqual(diag.stall_raw, [2048, 2128, 3417, 2146])
        self.assertEqual(diag.stall_flags, [0, 0, 0, 0])
        self.assertEqual(diag.stepper, (0, 0))
        self.assertEqual(len(diag.drivers), 2)

    def test_the_banks_are_named_from_the_standstill_experiment(self):
        ## F24 (select) dropped standstill on the F60 bank and F11 (feed) on
        ## the F50 bank, so these names are measured, not guessed.
        diag = D.read_diagnostics(FakeLink())
        self.assertEqual([d.label for d in diag.drivers],
                         [D.FEEDER, D.SELECTOR])
        self.assertIs(diag.feeder, diag.drivers[0])
        self.assertIs(diag.selector, diag.drivers[1])
        self.assertIsNone(diag.driver("nonexistent"))

    def test_standstill_and_current_scale(self):
        idle = D.DriverSnapshot(D.FEEDER, drv_status=0x80000000)
        self.assertFalse(idle.is_moving)
        self.assertEqual(idle.current_scale, 0)
        moving = D.DriverSnapshot(D.FEEDER, drv_status=0x00090000)
        self.assertTrue(moving.is_moving)
        self.assertEqual(moving.current_scale, 9)
        self.assertIsNone(D.DriverSnapshot(D.FEEDER).is_moving)

    def test_the_idle_board_is_healthy(self):
        ## reset + standstill are normal at idle and must not read as faults.
        diag = D.read_diagnostics(FakeLink())
        self.assertTrue(diag.is_healthy, diag.faults)
        self.assertIn("reset", diag.drivers[0].gstat_flags)
        self.assertIn("standstill", diag.drivers[0].drv_status_flags)

    def test_a_real_fault_is_reported_against_its_driver(self):
        script = dict(LIVE)
        script["F63"] = "DRV_STATUS: 00000002"   # driver 2 overtemp
        diag = D.read_diagnostics(FakeLink(script))
        self.assertFalse(diag.is_healthy)
        self.assertIn((D.SELECTOR, "overtemp"), diag.faults)
        self.assertTrue(diag.drivers[0].is_healthy)

    def test_f21_continuation_lines_are_used(self):
        ## F21's own payload is empty - reading only it loses every number.
        link = FakeLink()
        diag = D.read_diagnostics(link)
        self.assertIn("F21", link.asked)
        self.assertIsNotNone(diag.silk_raw)

    def test_f21_is_read_once(self):
        link = FakeLink()
        D.read_diagnostics(link)
        self.assertEqual(link.asked.count("F21"), 1)

    def test_a_silent_opcode_is_recorded_not_fatal(self):
        ## A board that will not answer F42 must still report driver faults.
        diag = D.read_diagnostics(FakeLink(fail={"F42", "F40"}))
        self.assertIn("F42", diag.errors)
        self.assertIn("F40", diag.errors)
        self.assertIsNone(diag.stepper)
        self.assertEqual(len(diag.drivers), 2)
        self.assertIsNotNone(diag.silk_raw)

    def test_only_query_opcodes_are_ever_sent(self):
        link = FakeLink()
        D.read_diagnostics(link)
        actuators = {"F10", "F11", "F15", "F18", "F23", "F24", "F39", "F112",
                     "F12", "F20", "F30", "F43"}
        self.assertEqual(actuators.intersection(link.asked), set())

    def test_marginal_channels(self):
        ## The point of F21: a barely-triggering channel is invisible in F13.
        diag = D.read_diagnostics(FakeLink())
        self.assertEqual(diag.marginal_channels(150, 400), [1, 2, 4])
        self.assertEqual(diag.marginal_channels(1000, 2000), [3])
        self.assertEqual(diag.marginal_channels(5000, 6000), [])

    def test_raw_silk_polarity_is_inverted(self):
        ## Measured: with channels 1/2/4 loaded and 3 empty, F21 read
        ## `silk: 199 333 1688 271`. Low means PRESENT. Getting this backwards
        ## would report the one empty channel as the only loaded one.
        diag = D.read_diagnostics(FakeLink())
        self.assertEqual(diag.loaded_by_raw(), [1, 2, 4])

    def test_raw_silk_agrees_with_the_f13_bitmask(self):
        ## F21's raw view and F13's thresholded view should name the same
        ## channels on a healthy board; divergence is the signal worth having.
        diag = D.read_diagnostics(FakeLink())
        self.assertEqual(diag.loaded_by_raw(), [1, 2, 4])

    def test_loaded_by_raw_takes_an_explicit_threshold(self):
        diag = D.read_diagnostics(FakeLink())
        self.assertEqual(diag.loaded_by_raw(threshold=100), [])
        self.assertEqual(diag.loaded_by_raw(threshold=2000), [1, 2, 3, 4])

    def test_as_dict_is_serialisable(self):
        import json
        json.dumps(D.read_diagnostics(FakeLink()).as_dict())


if __name__ == "__main__":
    unittest.main()


class TestReadDriverMotion(unittest.TestCase):
    """One register, for polling while something moves.

    read_diagnostics is fourteen queries and about two seconds of wire time;
    watching a selector jog needs the standstill bit and nothing else.
    """

    def test_it_reads_only_that_bank_s_drv_status(self):
        link = FakeLink()
        D.read_driver_motion(link, D.SELECTOR)
        self.assertEqual(link.asked, ["F63"])

    def test_the_feeder_bank_is_f53(self):
        link = FakeLink()
        D.read_driver_motion(link, D.FEEDER)
        self.assertEqual(link.asked, ["F53"])

    def test_standstill_reads_as_not_moving(self):
        ## 80000000 is bit 31, stst. The board reported exactly this at idle.
        link = FakeLink({"F63": "DRV_STATUS: 80000000"})
        self.assertIs(D.read_driver_motion(link, D.SELECTOR), False)

    def test_a_clear_standstill_bit_reads_as_moving(self):
        ## 00090000 is what the selector read while it was turning: stst gone,
        ## CS_ACTUAL 9 in bits 16-20.
        link = FakeLink({"F63": "DRV_STATUS: 00090000"})
        self.assertIs(D.read_driver_motion(link, D.SELECTOR), True)

    def test_an_unparsable_answer_is_unknown_not_moving(self):
        ## None and False must stay distinguishable: a driver that did not
        ## answer has not told us it arrived, and treating silence as arrival
        ## would end a jog wait early.
        link = FakeLink({"F63": ""})
        self.assertIsNone(D.read_driver_motion(link, D.SELECTOR))

    def test_an_unknown_bank_is_refused(self):
        with self.assertRaises(KeyError):
            D.read_driver_motion(FakeLink(), "flux capacitor")


class TestStreamIsLeftClean(unittest.TestCase):
    """A diagnostics read must not poison the next command.

    Measured on the printer: the F13 immediately after IFS_DIAGNOSTICS failed
    with a read error and the one after it succeeded, and the diagnostics output
    itself differed between calls - stall counts on one, raw silk on the next.
    Some opcode here answers with lines we do not model, and a line left in the
    buffer is handed to the next command as its reply. The poller's F13 is
    always the next command.
    """

    def test_it_drains_whatever_is_left(self):
        link = FakeLink()
        D.read_diagnostics(link)
        self.assertEqual(link.drains, 1)

    def test_the_drain_happens_after_every_query(self):
        link = FakeLink()
        D.read_diagnostics(link)
        self.assertEqual(link.asked[-1], "F42")

    def test_a_link_that_cannot_drain_still_reports(self):
        ## A board that will not answer is a diagnostic result, not a crash.
        link = FakeLink(drain_error="port gone")
        diag = D.read_diagnostics(link)
        self.assertIn("drain", diag.errors)
        self.assertEqual(diag.version, "3.0.6")

    def test_a_link_without_the_method_is_fine(self):
        ## Nothing should have to grow a method just to be read from.
        class Bare:
            capabilities = FakeCaps()

            def request(self, command):
                if command == "F21":
                    return FakeResponse("", list(F21_EXTRA))
                return FakeResponse(LIVE.get(command, ""))

        self.assertEqual(D.read_diagnostics(Bare()).version, "3.0.6")
