## load_cell_tare plugin tests: the H-protocol wire format, the serial link
## the backend keeps, and the weight sensor that feeds the collision watchdog.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock

from tests.ifs_klipper_fakes import FakeConfig, FakePrinter

ROOT = pathlib.Path(__file__).parents[1]
PLUGIN = ROOT / ".py" / "klipper" / "plugins" / "load_cell_tare.py"

H7_REPLY = b"command H7 ok. 8511562 140 g \r\n"
H1_REPLY = b"command H1 ok. 8511666 \r\n"


def load_plugin():
    spec = importlib.util.spec_from_file_location("load_cell_tare", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSerialPort:
    """pyserial's Serial, as far as the backend drives it."""

    def __init__(self, owner):
        self.owner = owner
        self.written = []
        self.closed = False

    def reset_input_buffer(self):
        pass

    def write(self, data):
        if self.owner.write_error is not None:
            raise self.owner.write_error
        self.written.append(bytes(data))

    def flush(self):
        pass

    def readline(self):
        reply = self.owner.replies.pop(0) if self.owner.replies else b""
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        self.closed = True


class FakeSerial:
    """The `serial` module, patched over sys.modules.

    `replies` is consumed one per readline; an entry may be bytes or an
    Exception to raise from the read. `write_error` raises from write() to
    stand in for a port that died under us. `constructed` counts opens.
    """

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.write_error = None
        self.constructed = 0
        self.ports = []

    def Serial(self, *args, **kwargs):
        port = FakeSerialPort(self)
        self.constructed += 1
        self.ports.append(port)
        return port


def make_serial_tare(serial, printer=None):
    """A LoadCellTareGcode on the serial transport, ready to be fired."""
    module = load_plugin()
    printer = printer or FakePrinter()
    for name in ("toolhead", "probe"):
        printer.objects.setdefault(name, types.SimpleNamespace())
    printer.objects.setdefault(
        "mod_params", types.SimpleNamespace(variables={}))
    tare = module.LoadCellTareGcode(FakeConfig("load_cell_tare", {
        "transport": "serial",
        "port": "/dev/ttyS7",
        "baud": "9600",
        "tolerance": "5.0",
        "timeout": "0.5",
    }, printer))
    printer.objects["load_cell_tare"] = tare
    return module, tare, printer


class ParseWeightTest(unittest.TestCase):
    def test_reads_grams_from_an_h7_reply(self):
        self.assertEqual(load_plugin().parse_weight(H7_REPLY), 140.0)

    def test_accepts_a_str_reply(self):
        self.assertEqual(
            load_plugin().parse_weight("command H7 ok. 1 28 g"), 28.0)

    def test_the_h1_reply_is_not_a_weight(self):
        self.assertIsNone(load_plugin().parse_weight(H1_REPLY))

    def test_empty_or_garbage_is_not_a_weight(self):
        module = load_plugin()
        for line in (b"", b"\r\n", b"ok", b"command H9 ok. 1 2 kg"):
            self.assertIsNone(module.parse_weight(line), line)

    def test_the_value_is_found_by_its_unit_not_its_column(self):
        module = load_plugin()
        self.assertEqual(
            module.parse_weight(b"command H7 ok. 0 140 g extra"), 140.0)


class SerialLinkTest(unittest.TestCase):
    def test_the_link_is_opened_once_and_kept(self):
        serial = FakeSerial([H7_REPLY, H7_REPLY])
        with mock.patch.dict(sys.modules, {"serial": serial}):
            _, tare, _ = make_serial_tare(serial)
            self.assertEqual(tare.read_weight(), 140.0)
            self.assertEqual(tare.read_weight(), 140.0)
        self.assertEqual(serial.constructed, 1)

    def test_a_dropped_link_is_reopened_by_the_next_exchange(self):
        serial = FakeSerial([H7_REPLY])
        with mock.patch.dict(sys.modules, {"serial": serial}):
            _, tare, _ = make_serial_tare(serial)
            self.assertEqual(tare.read_weight(), 140.0)

            serial.write_error = OSError("port died")
            with self.assertRaises(OSError):
                tare.read_weight()

            serial.write_error = None
            serial.replies.append(H7_REPLY)
            self.assertEqual(tare.read_weight(), 140.0)
        self.assertEqual(serial.constructed, 2)

    def test_a_garbled_reply_keeps_the_last_weight(self):
        serial = FakeSerial([H7_REPLY, b"garbled\r\n"])
        with mock.patch.dict(sys.modules, {"serial": serial}):
            _, tare, _ = make_serial_tare(serial)
            self.assertEqual(tare.read_weight(), 140.0)
            self.assertEqual(tare.read_weight(), 140.0)

    def test_tare_sends_h1_over_the_port(self):
        serial = FakeSerial([H1_REPLY])
        with mock.patch.dict(sys.modules, {"serial": serial}):
            _, tare, _ = make_serial_tare(serial)
            tare.backend.tare()
        self.assertEqual(serial.ports[0].written, [b"H1\n"])


class WeightSensorTest(unittest.TestCase):
    def _make(self, replies, sensor_options=None):
        self.serial = FakeSerial(replies)
        patcher = mock.patch.dict(sys.modules, {"serial": self.serial})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.module, self.tare, self.printer = make_serial_tare(self.serial)
        self.samples = []
        sensor = self.module.SerialWeightSensor(FakeConfig(
            "temperature_sensor weightValue",
            dict(sensor_options if sensor_options is not None
                 else {"report_time": "2.0"}), self.printer))
        sensor.setup_callback(
            lambda read_time, temp: self.samples.append((read_time, temp)))
        return sensor

    def test_the_poll_feeds_grams_to_the_temperature_callback(self):
        self._make([H7_REPLY])
        self.printer.fire("klippy:ready")
        self.assertEqual(len(self.printer.reactor.timers), 1)
        next_time = self.printer.reactor.timers[0](100.0)
        self.assertEqual(self.samples, [(100.0, 140.0)])
        self.assertEqual(next_time, 102.0)

    def test_a_failed_read_neither_samples_nor_kills_the_timer(self):
        self._make([OSError("no reply")])
        self.printer.fire("klippy:ready")
        next_time = self.printer.reactor.timers[0](100.0)
        self.assertEqual(self.samples, [])
        self.assertEqual(next_time, 102.0)

    def test_weight_is_read_through_the_tare_backends_link(self):
        self._make([H7_REPLY, H7_REPLY])
        self.printer.fire("klippy:ready")
        self.printer.reactor.timers[0](100.0)
        self.printer.reactor.timers[0](102.0)
        self.assertEqual(self.serial.constructed, 1)
        self.assertEqual(self.serial.ports[0].written, [b"H7\n", b"H7\n"])

    def test_without_a_tare_object_the_sensor_stays_idle(self):
        module = load_plugin()
        printer = FakePrinter()
        sensor = module.SerialWeightSensor(FakeConfig(
            "temperature_sensor weightValue", {}, printer))
        sensor.setup_callback(
            lambda *args: self.fail("sampled with no source attached"))
        printer.fire("klippy:ready")
        self.assertEqual(printer.reactor.timers, [])


class SensorFactoryTest(unittest.TestCase):
    def test_load_config_registers_the_serial_sensor_factory(self):
        module = load_plugin()
        printer = FakePrinter()
        registered = []
        printer.objects["heaters"] = types.SimpleNamespace(
            add_sensor_factory=lambda name, factory: registered.append(
                (name, factory)))
        obj = module.load_config(FakeConfig("load_cell_tare", {}, printer))
        self.assertIsInstance(obj, module.LoadCellTareGcode)
        self.assertEqual(
            registered, [("load_cell_serial", module.SerialWeightSensor)])


if __name__ == "__main__":
    unittest.main()
