"""Behaviour tests for the AD5X Klipper patch set.

`tools/klipper-merge/merge.sh` proves the AD5X files are the merge we think they
are. It cannot prove the merge is *correct*, and the two ways this merge goes
wrong are both silent:

- A side's behaviour is dropped in a region that merged clean. That happened to
  `statistics.py`, whose `disabled` option arrived without the guard that acts
  on it. Klippy started; the option did nothing.
- The two halves of the shaper interface disagree. That raises `TypeError` when
  a user runs input shaping, which is long after startup has succeeded.

So these tests assert the behaviours themselves: run the AD5X files, and check
statically what cannot be run off a printer.
"""

import ast
import importlib.util
from pathlib import Path

import pytest

from conftest import StubConfig, StubPrinter

REPO = Path(__file__).resolve().parents[2]
AD5X = REPO / ".py" / "klipper" / "patches.ad5x"
SHARED = REPO / ".py" / "klipper" / "patches"


def load(path, name):
    """Import a patch file by path; same-named files exist in both patch dirs."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree(path):
    return ast.parse(path.read_text())


# --------------------------------------------------------------------------
# virtual_sdcard: the print path, and on AD5X also the tool-change path
# --------------------------------------------------------------------------


class StubGCodeMacro:
    def load_template(self, config, name, default=None):
        return None


class StubPrintStats:
    def __init__(self):
        self.current_file = None

    def set_current_file(self, filename):
        self.current_file = filename


class SDPrinter(StubPrinter):
    """virtual_sdcard loads print_stats and gcode_macro, which differ in shape."""

    def load_object(self, config, name):
        if name == "gcode_macro":
            return self.objects.setdefault(name, StubGCodeMacro())
        return self.objects.setdefault(name, StubPrintStats())


class SDCommand:
    def __init__(self, **params):
        self._params = params
        self.raw = []

    def get_int(self, key, default=None):
        return int(self._params.get(key, default))

    def get_raw_command_parameters(self):
        return self._params.get("raw", "")

    def respond_raw(self, msg):
        self.raw.append(msg)

    def error(self, msg):
        return RuntimeError(msg)


@pytest.fixture
def virtual_sd(tmp_path):
    module = load(AD5X / "extras" / "virtual_sdcard.py", "ad5x_virtual_sdcard")
    printer = SDPrinter()
    config = StubConfig({"path": str(tmp_path)}, printer=printer)
    return module, module.VirtualSD(config), printer, tmp_path


def test_status_carries_both_the_ifs_and_the_forgex_fields(virtual_sd):
    """Both sides add state here. Losing either side's keys breaks a consumer:
    the panel reads channel/refuelling, Forge-X's UI reads estimate_print_time."""
    _, sd, _, _ = virtual_sd

    status = sd.get_status(0.0)

    assert status["channel"] == 0
    assert status["refuelling"] is False
    assert "estimate_print_time" in status


def test_ifs_gcode_commands_are_registered(virtual_sd):
    """The AD5X filament system is driven through these three commands."""
    _, _, printer, _ = virtual_sd

    registered = printer.lookup_object("gcode").commands

    assert "SDCARD_SET_CHANNEL" in registered
    assert "SDCARD_ENABLE_FFM" in registered
    assert "SDCARD_CLEAR_REFUELLING" in registered


def test_enabling_ffm_arms_the_tool_change_watcher(virtual_sd):
    """enable_ffm is what makes the print loop act on T0-T15 mid-file."""
    _, sd, printer, _ = virtual_sd

    assert sd.enable_ffm is False
    printer.lookup_object("gcode").commands["SDCARD_ENABLE_FFM"](
        SDCommand(ENABLE=1)
    )

    assert sd.enable_ffm is True


def test_the_tool_change_watcher_covers_sixteen_tools(virtual_sd):
    """Stock AD5X dispatches on this list from inside the print loop."""
    module, _, _, _ = virtual_sd

    assert module.VALID_GCODE_T[0] == "T0"
    assert module.VALID_GCODE_T[-1] == "T15"
    assert len(module.VALID_GCODE_T) == 16


def test_gx_files_are_listable(virtual_sd):
    """FlashForge's own slicer writes .gx and the stock UI puts it on the disk.
    Forge-X drops the extension on AD5M; dropping it here would hide the files."""
    module, sd, _, tmp = virtual_sd
    (tmp / "part.gx").write_text("G1 X0\n")

    assert "gx" in module.VALID_GCODE_EXTS
    assert [f[0] for f in sd.get_file_list()] == ["part.gx"]


def test_hidden_files_and_directories_are_skipped(virtual_sd):
    """Forge-X's fix. Without it a recursive listing surfaces macOS and
    thumbnail droppings as printable files."""
    _, sd, _, tmp = virtual_sd
    (tmp / "visible.gcode").write_text("G1\n")
    (tmp / ".hidden.gcode").write_text("G1\n")
    (tmp / ".trash").mkdir()
    (tmp / ".trash" / "buried.gcode").write_text("G1\n")

    listed = [f[0] for f in sd.get_file_list(check_subdirs=True)]

    assert listed == ["visible.gcode"]


def test_filename_lookup_falls_back_to_a_case_insensitive_match(virtual_sd):
    """FlashForge commented this out on both platforms; Forge-X turned it back
    on, so a file uploaded as Part.gcode still opens when asked for as
    part.gcode. It is the one region where we take Forge-X over AD5X stock."""
    _, sd, _, tmp = virtual_sd
    (tmp / "Part.gcode").write_text("G1 X0\n")

    sd._load_file(SDCommand(), "part.gcode")

    assert sd.current_file is not None
    assert sd.current_file.name.endswith("Part.gcode")


# --------------------------------------------------------------------------
# statistics: the option that arrived without its guard
# --------------------------------------------------------------------------


@pytest.fixture
def statistics():
    return load(AD5X / "extras" / "statistics.py", "ad5x_statistics")


class StatsReactor:
    def __init__(self):
        self.registered = []

    def register_timer(self, callback):
        self.registered.append(callback)
        return object()

    def monotonic(self):
        return 0.0


class StatsPrinter(StubPrinter):
    def __init__(self):
        super().__init__()
        self.reactor = StatsReactor()

    def get_reactor(self):
        return self.reactor


def stats_config(disabled):
    return StubConfig({"disabled": disabled}, printer=StatsPrinter())


def test_disabled_actually_suppresses_the_stats_timer(statistics):
    """The regression this whole file exists for. The mechanical merge kept
    Forge-X's `disabled` option and dropped the `if not self.disabled:` around
    the registration, because AD5X had reordered the lines beside it. Nothing
    failed: the timer still ran and only the log line went quiet."""
    printer = StatsPrinter()

    statistics.PrinterStats(StubConfig({"disabled": True}, printer=printer))

    assert printer.reactor.registered == []
    assert "klippy:ready" not in printer.event_handlers


def test_enabled_still_registers_the_stats_timer(statistics):
    printer = StatsPrinter()

    statistics.PrinterStats(StubConfig({"disabled": False}, printer=printer))

    assert len(printer.reactor.registered) == 1
    assert "klippy:ready" in printer.event_handlers


def test_stats_run_on_the_ad5x_two_second_cadence(statistics):
    """AD5X stock halved the stats rate Forge-X inherited from upstream. This
    platform shuts the MCU down under host load, so the slower one is kept."""
    stats = statistics.PrinterStats(stats_config(False))
    stats.stats_cb = [lambda eventtime: (0, "")]

    assert stats.generate_stats(100.0) == 102.0


# --------------------------------------------------------------------------
# the shaper pair: two files, one interface
# --------------------------------------------------------------------------


def method_signatures(path, class_name):
    sigs = {}
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    a = item.args
                    names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
                    sigs[item.name] = {
                        "names": names,
                        "positional": len(a.posonlyargs) + len(a.args),
                        "takes_kwargs": a.kwarg is not None,
                    }
    return sigs


def calls_to(path, names, receivers):
    """Calls of `<receiver>.<name>(...)`.

    The receiver matters: resonance_tester has its own save_calibration_data
    with a different signature, and checking it against ShaperCalibrate's would
    be comparing two unrelated methods that happen to share a name.
    """
    for node in ast.walk(tree(path)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in names:
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id in receivers:
            yield node


@pytest.mark.parametrize(
    "caller, receivers",
    [
        # resonance_tester holds a ShaperCalibrate as `helper` and receives one
        # as the `shaper_calibrate` parameter.
        ("extras/resonance_tester.py", {"helper", "shaper_calibrate"}),
        # shaper_calibrate *is* ShaperCalibrate, so it reaches itself as self.
        ("extras/shaper_calibrate.py", {"self"}),
    ],
)
def test_every_shaper_call_matches_the_signature_it_reaches(caller, receivers):
    """AD5X ships a newer shaper API than Forge-X was written against, and the
    two files must be merged together. A mismatch here is not a load error: it
    raises TypeError only when someone runs SHAPER_CALIBRATE."""
    sigs = method_signatures(AD5X / "extras" / "shaper_calibrate.py", "ShaperCalibrate")
    assert "find_best_shaper" in sigs and "save_calibration_data" in sigs

    checked = 0
    for call in calls_to(AD5X / caller, set(sigs), receivers):
        sig = sigs[call.func.attr]
        checked += 1

        for kw in call.keywords:
            if kw.arg is None:
                continue
            assert kw.arg in sig["names"] or sig["takes_kwargs"], (
                "%s passes %s=, which %s() does not accept"
                % (caller, kw.arg, call.func.attr)
            )

        # +1 for self, which the call site supplies as the receiver.
        assert len(call.args) + 1 <= sig["positional"], (
            "%s passes %d positional arguments to %s(), which takes %d"
            % (caller, len(call.args), call.func.attr, sig["positional"] - 1)
        )

        # Arity alone is not enough, and assuming it was is how this test first
        # went out useless. AD5X's find_best_shaper takes nine optional
        # parameters, so Forge-X's older positional call -- (data, max_smoothing,
        # respond_info, scv=scv) -- fits it comfortably and binds max_smoothing
        # to `shapers` and the logger to `damping_ratio`. No TypeError, just
        # wrong answers out of input shaping.
        #
        # So check the binding, not the count: a positional argument named after
        # some *other* parameter of the function it is calling is landing in the
        # wrong slot. An argument whose name matches nothing is a local alias and
        # tells us nothing either way.
        for i, arg in enumerate(call.args):
            if not isinstance(arg, ast.Name):
                continue
            expected = sig["names"][i + 1] if i + 1 < len(sig["names"]) else None
            if arg.id == expected:
                continue
            assert arg.id not in sig["names"], (
                "%s passes %s positionally to %s(), where position %d is %s"
                % (caller, arg.id, call.func.attr, i + 1, expected)
            )

    assert checked, "no shaper calls found in %s - the test stopped reaching them" % caller


def test_max_freq_reaches_the_calibration_writer():
    """AD5X's addition. It has to survive the merge with Forge-X's JSON export,
    which is why that region takes both sides rather than one."""
    source = (AD5X / "extras" / "resonance_tester.py").read_text()

    assert "max_freq=max_freq," in source
    assert "all_shapers, max_freq)" in source


def test_the_json_export_still_feeds_zshaper():
    """Forge-X's JSON dump is not decoration: .root/zshaper/calibrate_shaper.py
    plots from it and reads these keys by name. Dropping the export, or a field
    inside it, breaks the shaper graphs with a KeyError at plot time."""
    reader = REPO / ".root" / "zshaper" / "calibrate_shaper.py"
    wanted = {"data": set(), "shaper": set()}

    for node in ast.walk(tree(reader)):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in wanted
            and isinstance(node.slice, ast.Constant)
        ):
            wanted[node.value.id].add(node.slice.value)

    assert wanted["data"] and wanted["shaper"], "zshaper stopped reading the JSON"

    produced = set()
    for node in ast.walk(tree(AD5X / "extras" / "resonance_tester.py")):
        if isinstance(node, ast.Dict):
            produced |= {
                k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }

    missing = (wanted["data"] | wanted["shaper"]) - produced
    assert not missing, "resonance_tester no longer exports: %s" % sorted(missing)


def test_the_forgex_pipe_drain_fix_survives():
    """Forge-X's fix for a child process that blocks forever on a full pipe when
    a calibration result is large. It sits outside every conflict region, which
    is exactly why it could be lost without anything complaining."""
    source = (AD5X / "extras" / "shaper_calibrate.py").read_text()

    assert "parent_conn.poll()" in source


# --------------------------------------------------------------------------
# shape of the overlay
# --------------------------------------------------------------------------


def test_files_whose_stock_is_identical_are_not_duplicated():
    """These three have byte-identical stock on both platforms, so Forge-X's own
    patch is correct for AD5X. A copy here would be a second thing to maintain
    that nothing would tell us had gone stale."""
    for name in ("gcode_move.py", "led.py", "temperature_sensor.py"):
        assert (SHARED / "extras" / name).exists()
        assert not (AD5X / "extras" / name).exists()
