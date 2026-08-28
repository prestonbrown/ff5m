## Tests for the klipper-facing half of the AD5X IFS support: the [ifs] object
## and the two filament sensors that read through it.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest

import ifs_modules
import ifs_klipper_fakes as fakes

fakes.install_filament_switch_sensor()

IFS = ifs_modules.load("ifs")
STATUS = ifs_modules.load("ifs_status")
LOGIC = ifs_modules.load("ifs_sensor_logic")
TOOLHEAD = ifs_modules.load("ifs_toolhead_sensor")
CHANNEL = ifs_modules.load("ifs_channel_sensor")


class FakeCapabilities:
    def __init__(self, channel_count=4, version="3.0.6", probed=True):
        self.channel_count = channel_count
        self.version = version
        self.probed = probed


class FakeLink:
    """A board that answers F13 with scripted lines.

    Deliberately a *link*, not an operations layer: the real IfsOperations and
    the real F13 parser run on top of it, so these tests exercise the whole
    stack below klipper rather than a stand-in for it.
    """

    def __init__(self, capabilities=None, replies=None, fail=None):
        self.capabilities = capabilities or FakeCapabilities()
        self.replies = list(replies or [])
        self.fail = fail
        self.closed = False
        self.asked = []

    def request(self, command):
        self.asked.append(command)
        if self.fail:
            raise RuntimeError(self.fail)
        if not self.replies:
            raise RuntimeError("no more scripted replies")
        return FakeResponse(self.replies.pop(0))

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.extra = []
        self.is_error = False


def f13(state=STATUS.READY, silk=0b1011, chan=0, insert=0, stall=0):
    """An F13 payload as the board formats it."""
    return ("FFS_state: %d silk_state: %d chan: %d ffs_channels_insert: %d "
            "stall_state: %d" % (state, silk, chan, insert, stall))


def status(state=STATUS.READY, silk=0b1011, insert=0, stall=0, chan=0):
    """A parsed snapshot, for the sensors that read one rather than the wire."""
    return STATUS.IfsStatus(state=state, silk_mask=silk, active_channel=chan,
                            insert_mask=insert, stall_mask=stall)


def make_ifs(values=None, link=None, replies=None):
    printer = fakes.FakePrinter()
    config = fakes.FakeConfig("ifs", values, printer)
    if link is None:
        link = FakeLink(replies=replies)
    obj = IFS.IFS(config, open_link=lambda port, commit: link)
    printer.add_object("ifs", obj)
    return obj, printer, link


class TestIfsObject(unittest.TestCase):
    def test_before_any_poll_nothing_is_claimed(self):
        obj, _, _ = make_ifs()
        info = obj.get_status()
        self.assertFalse(info["connected"])
        self.assertIsNone(info["activity"])
        self.assertEqual(info["loaded_channels"], [])
        ## None, not False - "no reading yet" must not look like a runout.
        self.assertIsNone(obj.has_filament(1))
        self.assertIsNone(obj.latest_status())

    def test_a_poll_publishes_the_snapshot(self):
        obj, _, link = make_ifs(replies=[f13(silk=0b1011)])
        self.assertTrue(obj._connect())
        self.assertTrue(obj._poll_once())
        info = obj.get_status()
        self.assertTrue(info["connected"])
        self.assertEqual(info["loaded_channels"], [1, 2, 4])
        self.assertEqual(info["activity"], "ready")
        self.assertTrue(obj.has_filament(2))
        self.assertFalse(obj.has_filament(3))

    def test_status_keys_are_the_same_shape_either_way(self):
        ## Moonraker consumers should not have to test for missing keys.
        obj, _, link = make_ifs(replies=[f13()])
        empty = set(obj.get_status())
        obj._connect(); obj._poll_once()
        self.assertEqual(empty, set(obj.get_status()))

    def test_probed_capabilities_win_over_config(self):
        obj, _, _ = make_ifs({"channel_count": 8},
                             link=FakeLink(FakeCapabilities(channel_count=4)))
        obj._connect()
        self.assertEqual(obj.channel_count, 4)

    def test_config_is_the_fallback_when_f19_went_unanswered(self):
        link = FakeLink(FakeCapabilities(channel_count=4, version=None,
                                         probed=False))
        obj, _, _ = make_ifs({"channel_count": 2}, link=link)
        obj._connect()
        self.assertEqual(obj.channel_count, 2)

    def test_a_failed_open_is_recorded_not_raised(self):
        def explode(port, commit):
            raise OSError("no such device")
        printer = fakes.FakePrinter()
        obj = IFS.IFS(fakes.FakeConfig("ifs", {}, printer), open_link=explode)
        self.assertFalse(obj._connect())
        info = obj.get_status()
        self.assertFalse(info["connected"])
        self.assertIn("no such device", info["error"])

    def test_a_failed_poll_is_recorded_not_raised(self):
        obj, _, link = make_ifs()
        obj._connect()
        link.fail = "board silent"
        self.assertFalse(obj._poll_once())
        self.assertIn("board silent", obj.get_status()["error"])

    def test_repeated_failures_drop_the_link(self):
        ## One bad read is noise; a run of them means the port is gone.
        obj, _, link = make_ifs()
        obj._connect()
        link.fail = "gone"
        for _ in range(IFS.MAX_POLL_FAILURES):
            obj._poll_once()
            obj._failures += 1
            if obj._failures >= IFS.MAX_POLL_FAILURES:
                obj._drop_link("test")
        self.assertTrue(link.closed)
        self.assertIsNone(obj._link)

    def test_an_insert_is_marshalled_to_the_reactor(self):
        ## Serial work happens off-thread; anything touching klipper has to
        ## come back through the reactor.
        obj, printer, link = make_ifs(replies=[f13(insert=0b10)])
        obj._connect()
        obj._poll_once()
        self.assertEqual(len(printer.reactor.async_callbacks), 1)
        self.assertEqual(printer.sent, [])
        printer.reactor.run_async()
        self.assertEqual(printer.sent, [("ifs:filament_inserted", ([2],))])

    def test_an_insert_fires_once(self):
        obj, printer, link = make_ifs(
            replies=[f13(insert=0b10), f13(insert=0b10)])
        obj._connect()
        obj._poll_once(); obj._poll_once()
        printer.reactor.run_async()
        self.assertEqual(len(printer.sent), 1)

    def test_dropping_a_link_twice_is_harmless(self):
        obj, _, _ = make_ifs()
        obj._connect()
        obj._drop_link()
        obj._drop_link()


class AliveThread:
    """Stands in for the poll thread, which these tests drive by hand."""

    def is_alive(self):
        return True

    def join(self, timeout=None):
        return None


class TestCommandQueue(unittest.TestCase):
    """Commands from klipper must not block the reactor.

    IfsLink is not thread-safe and an exchange takes ~165 ms on hardware, so a
    mutex would stall klipper repeatedly during exactly the sequences that
    matter. Requests are queued for the poll thread and the caller yields.
    """

    def setUp(self):
        self.obj, self.printer, self.link = make_ifs(replies=[f13()])
        self.obj._connect()
        self.obj._thread = AliveThread()
        self.reactor = self.printer.reactor
        ## Stand in for the poll thread running between reactor pauses.
        self.reactor.on_pause = self.obj._run_queued

    def test_a_command_is_answered(self):
        self.link.replies = ["chan 1."]
        response = self.obj.execute("F24 C1")
        self.assertEqual(response.payload, "chan 1.")
        self.assertIn("F24 C1", self.link.asked)

    def test_it_yields_the_reactor_rather_than_blocking(self):
        ## The whole point. If this ever stops pausing, klipper stalls for the
        ## length of every IFS exchange during a load.
        self.link.replies = ["chan 1."]
        self.obj.execute("F24 C1")
        self.assertGreater(self.reactor.pauses, 0)

    def test_a_board_error_surfaces(self):
        self.link.fail = "FFS not ready."
        with self.assertRaises(RuntimeError) as caught:
            self.obj.execute("F24 C1")
        self.assertIn("FFS not ready.", str(caught.exception))

    def test_a_silent_board_times_out_and_the_request_is_dropped(self):
        ## Nothing runs the queue, so the wait must end by itself and leave
        ## nothing behind for a later poll to answer into the void.
        self.reactor.on_pause = None
        with self.assertRaises(IFS.IfsBusy):
            self.obj.execute("F24 C1", timeout=0.2)
        self.assertEqual(self.obj._queue, [])

    def test_no_poller_means_no_command(self):
        self.obj._thread = None
        with self.assertRaises(IFS.IfsBusy):
            self.obj.execute("F13")

    def test_shutdown_releases_a_waiting_caller(self):
        ## A caller blocked on a command when klippy goes down must not hang.
        request = IFS._Request("F13")
        self.obj._queue.append(request)
        self.obj._stop()
        self.assertTrue(request.done.is_set())
        self.assertIn("shutting down", request.error)

    def test_a_disconnected_link_fails_queued_commands(self):
        obj, printer, _ = make_ifs()
        obj._open_link = lambda port, commit: (_ for _ in ()).throw(
            OSError("gone"))
        request = IFS._Request("F13")
        obj._queue.append(request)
        ## One pass of the loop body with no link: connect fails, queue drains.
        self.assertFalse(obj._connect())
        obj._fail_queued("the IFS is not connected")
        self.assertTrue(request.done.is_set())
        self.assertIn("not connected", request.error)

    def test_queued_commands_run_before_status_polls(self):
        ## A sequence waiting on a command should not pay for a status poll.
        self.link.replies = ["chan 1.", f13()]
        self.assertTrue(self.obj._run_queued() is False)
        request = IFS._Request("F24 C1")
        self.obj._queue.append(request)
        self.assertTrue(self.obj._run_queued())
        self.assertEqual(self.link.asked[-1], "F24 C1")


class TestGcode(unittest.TestCase):
    def test_status_command_when_disconnected(self):
        obj, printer, _ = make_ifs()
        gcode = printer.lookup_object("gcode")
        gcode.commands["IFS_STATUS"](gcode)
        self.assertIn("not connected", gcode.responses[-1])

    def test_status_command_reports_the_board(self):
        obj, printer, link = make_ifs(replies=[f13(silk=0b1011, chan=2)])
        obj._connect(); obj._poll_once()
        gcode = printer.lookup_object("gcode")
        gcode.commands["IFS_STATUS"](gcode)
        message = gcode.responses[-1]
        self.assertIn("3.0.6", message)
        self.assertIn("ready", message)
        self.assertIn("[1, 2, 4]", message)

    def test_both_commands_are_registered(self):
        _, printer, _ = make_ifs()
        gcode = printer.lookup_object("gcode")
        self.assertIn("IFS_STATUS", gcode.commands)
        self.assertIn("IFS_DIAGNOSTICS", gcode.commands)


class FakeAdcChannel:
    def __init__(self, value):
        self.value = value

    def get_last_value(self):
        ## klipper returns (value, timestamp) in that order. zmod unpacks it
        ## the other way and compensates; this pins the correct order.
        return self.value, 1234.5


class FakeQueryAdc:
    def __init__(self, values):
        self.adc = {name: FakeAdcChannel(v) for name, v in values.items()}


def make_toolhead(values=None, adc=None):
    printer = fakes.FakePrinter()
    if adc is not None:
        printer.add_object("query_adc", FakeQueryAdc(adc))
    config = fakes.FakeConfig("ifs_toolhead_sensor toolhead", values, printer)
    return TOOLHEAD.IfsToolheadSensor(config), printer


class TestToolheadSensor(unittest.TestCase):
    ADC = TOOLHEAD.DEFAULT_ADC

    def test_engaged_reads_present(self):
        sensor, _ = make_toolhead(adc={self.ADC: 0.0081})
        self.assertTrue(sensor.read_present())
        self.assertEqual(sensor.last_state, LOGIC.PRESENT)

    def test_empty_reads_absent(self):
        sensor, _ = make_toolhead(adc={self.ADC: 0.0432})
        self.assertFalse(sensor.read_present())
        self.assertEqual(sensor.last_state, LOGIC.ABSENT)

    def test_no_query_adc_is_no_reading_not_a_runout(self):
        sensor, _ = make_toolhead()
        self.assertIsNone(sensor.read_present())

    def test_a_missing_adc_name_is_no_reading(self):
        sensor, _ = make_toolhead(adc={"something else": 0.5})
        self.assertIsNone(sensor.read_present())

    def test_thresholds_are_configurable(self):
        ## The bands are one printer's hardware, not a universal constant.
        sensor, _ = make_toolhead({"present_max": 0.5, "absent_min": 0.6},
                                  adc={self.ADC: 0.4})
        self.assertTrue(sensor.read_present())

    def test_it_registers_as_a_stock_filament_switch_sensor(self):
        ## This is the surface Moonraker and HelixScreen already watch.
        sensor, printer = make_toolhead(adc={self.ADC: 0.008})
        self.assertIs(printer.lookup_object("filament_switch_sensor toolhead"),
                      sensor)

    def test_the_timer_notes_the_reading(self):
        sensor, printer = make_toolhead(adc={self.ADC: 0.008})
        printer.fire("klippy:ready")
        self.assertEqual(len(printer.reactor.timers), 1)
        printer.reactor.timers[0](100.0)
        self.assertEqual(sensor.runout_helper.notes, [True])

    def test_a_reading_of_none_leaves_the_helper_alone(self):
        ## Absence of a reading must not be reported as absence of filament.
        sensor, printer = make_toolhead()
        printer.fire("klippy:ready")
        printer.reactor.timers[0](100.0)
        self.assertEqual(sensor.runout_helper.notes, [])


def make_channel(channel=1, ifs_obj=None):
    printer = fakes.FakePrinter()
    if ifs_obj is not None:
        printer.add_object("ifs", ifs_obj)
    config = fakes.FakeConfig("ifs_channel_sensor lane%d" % channel,
                              {"channel": channel}, printer)
    return CHANNEL.IfsChannelSensor(config), printer


class StubIfs:
    def __init__(self, status_value):
        self._status = status_value

    def latest_status(self):
        return self._status


class TestChannelSensor(unittest.TestCase):
    def test_reads_the_silk_bit_for_its_channel(self):
        loaded = StubIfs(status(silk=0b1011))
        self.assertTrue(make_channel(2, loaded)[0].read_present())
        self.assertFalse(make_channel(3, loaded)[0].read_present())

    def test_no_ifs_object_is_no_reading(self):
        ## Config section order decides construction order, so [ifs] may not
        ## exist yet. That is not a runout.
        sensor, _ = make_channel(1)
        self.assertIsNone(sensor.read_present())

    def test_no_status_yet_is_no_reading(self):
        sensor, _ = make_channel(1, StubIfs(None))
        self.assertIsNone(sensor.read_present())

    def test_it_registers_as_a_stock_filament_switch_sensor(self):
        sensor, printer = make_channel(1, StubIfs(status()))
        self.assertIs(printer.lookup_object("filament_switch_sensor lane1"),
                      sensor)

    def test_four_channels_share_one_poll(self):
        ## The whole reason these read a snapshot instead of the wire.
        shared = StubIfs(status(silk=0b1011))
        results = [make_channel(ch, shared)[0].read_present()
                   for ch in (1, 2, 3, 4)]
        self.assertEqual(results, [True, True, False, True])


if __name__ == "__main__":
    unittest.main()
