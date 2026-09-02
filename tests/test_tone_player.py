## A buzzer must never take the printer down.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest

import ifs_klipper_fakes as fakes
import ifs_modules


T = ifs_modules.load("tone_player")


def make_player(available=True, values=None):
    printer = fakes.FakePrinter()
    config = fakes.FakeConfig("tone_player", values, printer)
    player = T.TonePlayer(config)
    player.available = available
    return player, printer


class TestBuzzerDetection(unittest.TestCase):
    def test_a_board_with_no_pwm_has_no_buzzer(self):
        """The AD5X has no /sys/class/pwm at all.

        Its buzzer belongs to the stock firmwareExe process, not to Linux, so
        the directory the player writes to simply is not there.
        """
        self.assertFalse(T.buzzer_available(chip=98765))


class TestTonePlayer(unittest.TestCase):
    def test_no_buzzer_plays_nothing_and_raises_nothing(self):
        player, _ = make_player(available=False)
        played = []
        player._play = lambda notes: played.append(notes)
        player.cmd_TONE(fakes.FakeGcmd({"NOTES": "440:100"}))
        self.assertEqual(played, [])

    def test_a_failing_buzzer_does_not_shut_the_printer_down(self):
        """The bug this exists for.

        Klipper turns any non-gcode exception into "Internal error on command",
        which shuts klippy down and takes the MCUs with it. A completed filament
        change ended in exactly that, and needed a FIRMWARE_RESTART, because the
        chirp afterwards hit a PWM device that does not exist.
        """
        player, _ = make_player()

        def boom(notes):
            raise FileNotFoundError("/sys/class/pwm/pwmchip0/export")

        player._play = boom
        player.cmd_TONE(fakes.FakeGcmd({"NOTES": "440:100"}))   # must not raise

    def test_it_stops_trying_after_the_first_failure(self):
        ## A buzzer that failed once fails every time, and a warning per note
        ## during a print is its own kind of broken.
        player, _ = make_player()
        calls = []

        def boom(notes):
            calls.append(notes)
            raise OSError("no such device")

        player._play = boom
        for _ in range(3):
            player.cmd_TONE(fakes.FakeGcmd({"NOTES": "440:100"}))
        self.assertEqual(len(calls), 1)
        self.assertFalse(player.available)

    def test_a_working_buzzer_still_plays(self):
        player, _ = make_player()
        played = []
        player._play = lambda notes: played.append(notes)
        player.cmd_TONE(fakes.FakeGcmd({"NOTES": "440:100 220:50"}))
        self.assertEqual(len(played), 1)
        self.assertEqual(played[0], [(440, 100), (220, 50)])
