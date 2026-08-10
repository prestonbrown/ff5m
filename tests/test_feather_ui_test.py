## Host-side contracts for the lazy on-printer Feather UI runner.

import json
import pathlib
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
PLUGINS = ROOT / ".py" / "klipper" / "plugins"

import sys
sys.path.insert(0, str(PLUGINS))

import feather_feature_ui_test as UI_TEST  # noqa: E402
import feather_operation_context_fixtures as CONTEXT_FIXTURES  # noqa: E402
import feather_screen as FEATHER  # noqa: E402
from feather_feature_manager import LazyFeatureManager  # noqa: E402
from tests.visual_checks import hybrid as HYBRID  # noqa: E402


class AsyncReactor:
    def register_async_callback(self, callback):
        callback(1.0)


class GCmd:
    def __init__(self, values=None):
        self.values = values or {}
        self.responses = []

    def get(self, name, default=None):
        return self.values.get(name, default)

    def get_int(self, name, default=0):
        return int(self.values.get(name, default))

    def respond_info(self, message):
        self.responses.append(message)

    def error(self, message):
        return RuntimeError(message)


class ArtifactWorkerTest(unittest.TestCase):
    def test_frame_capture_requires_continuous_quiet_window(self):
        worker = object.__new__(UI_TEST.ArtifactWorker)
        frames = [b"first", b"first", b"second", b"second",
                  b"second", b"second"]
        worker._read_frame = mock.Mock(side_effect=frames)

        with mock.patch.object(UI_TEST, "FRAME_SETTLE_INTERVAL", 0.15), \
                mock.patch.object(UI_TEST, "FRAME_SETTLE_TIMEOUT", 2.0), \
                mock.patch.object(UI_TEST.time, "monotonic", side_effect=(
                    0.0, 0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30)), \
                mock.patch.object(UI_TEST.time, "sleep") as sleep:
            data, digest = worker._stable_frame()

        self.assertEqual(data, b"second")
        self.assertEqual(digest, UI_TEST.hashlib.sha256(b"second").hexdigest())
        self.assertEqual(worker._read_frame.call_count, 6)
        self.assertEqual(sleep.call_count, 5)

    def test_frame_capture_reads_the_active_virtual_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            framebuffer = root / "fb0"
            pan = root / "pan"
            stride = root / "stride"
            first = b"\x11\x22\x33\xff" * (
                UI_TEST.SCREEN_WIDTH * UI_TEST.SCREEN_HEIGHT)
            second = b"\xaa\xbb\xcc\xff" * (
                UI_TEST.SCREEN_WIDTH * UI_TEST.SCREEN_HEIGHT)
            framebuffer.write_bytes(first + second)
            pan.write_text("0,480\n", encoding="ascii")
            stride.write_text("3200\n", encoding="ascii")
            worker = object.__new__(UI_TEST.ArtifactWorker)
            worker.framebuffer = str(framebuffer)
            worker.framebuffer_pan = str(pan)
            worker.framebuffer_stride = str(stride)

            self.assertEqual(worker._read_frame(), second)

    def test_frame_capture_retries_if_page_flips_during_read(self):
        worker = object.__new__(UI_TEST.ArtifactWorker)
        first = b"first"
        second = b"second"
        worker._read_stride = mock.Mock(return_value=3200)
        worker._read_pan = mock.Mock(side_effect=(
            (0, 0), (0, 480), (0, 480), (0, 480)))
        worker._read_frame_at = mock.Mock(side_effect=(first, second))

        self.assertEqual(worker._read_frame(), second)
        self.assertEqual(worker._read_frame_at.call_count, 2)

    def test_writes_bmp_manifest_telemetry_and_log_slice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            run = root / "20260729-120000-ui"
            run.mkdir()
            framebuffer = root / "fb0"
            pixels = bytearray(UI_TEST.FRAME_BYTES)
            pixels[0:4] = b"\x03\x06\x07\xff"
            framebuffer.write_bytes(pixels)
            printer_log = root / "printer.log"
            printer_log.write_text("before\n", encoding="utf-8")
            active = root / "active.json"
            active.write_text("{}\n", encoding="utf-8")

            worker = UI_TEST.ArtifactWorker(
                AsyncReactor(), str(run), str(framebuffer), str(printer_log))
            printer_log.write_text("before\nafter\n", encoding="utf-8")
            captured = []
            captured_event = threading.Event()
            worker.capture(
                1, "Main menu", {"page": "MAIN_MENU"},
                lambda value: (captured.append(value), captured_event.set()))
            self.assertTrue(captured_event.wait(3.0))
            self.assertFalse(isinstance(captured[0], Exception))

            worker.telemetry(
                "positions", ("time", "x", "y", "z"),
                {"time": 1, "x": 2, "y": 3, "z": 4})
            worker.log("finished phase")
            finished = []
            finished_event = threading.Event()
            with mock.patch.object(UI_TEST, "ACTIVE_MARKER", str(active)):
                worker.finish(
                    {"outcome": "passed",
                     "operation_context": {
                         "passed": True, "scenario_count": 0,
                         "scenarios": []},
                     "_operation_context_artifact": {
                         "passed": True, "scenarios": []}},
                    lambda value: (finished.append(value),
                                   finished_event.set()))
                self.assertTrue(finished_event.wait(3.0))
            worker.stop()

            bmp = run / captured[0]["file"]
            self.assertEqual(bmp.read_bytes()[:2], b"BM")
            self.assertEqual(bmp.stat().st_size, UI_TEST.FRAME_BYTES + 54)
            manifest = json.loads((run / "manifest.json").read_text())
            self.assertEqual(manifest[0]["page"], "MAIN_MENU")
            self.assertTrue(manifest[0]["passed"])
            self.assertIn("time,x,y,z", (run / "positions.csv").read_text())
            self.assertIn("finished phase", (run / "run.log").read_text())
            self.assertEqual((run / "printer.log").read_text(), "after\n")
            operation = json.loads(
                (run / "operation_context.json").read_text())
            self.assertTrue(operation["passed"])
            summary = json.loads((run / "summary.json").read_text())
            self.assertNotIn("_operation_context_artifact", summary)
            self.assertTrue(summary["operation_context"]["passed"])
            self.assertFalse(active.exists())

    def test_retention_preserves_newest_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            current = root / "20260729-120004-000001-ui"
            current.mkdir()
            (current / "summary.json").write_text(
                '{"outcome":"passed"}', encoding="utf-8")
            names = []
            for index in range(4):
                name = "20260729-12000%d-000001-ui" % index
                names.append(name)
                directory = root / name
                directory.mkdir()
                outcome = "failed" if index == 3 else "passed"
                (directory / "summary.json").write_text(
                    json.dumps({"outcome": outcome}), encoding="utf-8")
            worker = UI_TEST.ArtifactWorker(
                AsyncReactor(), str(current), "/missing-fb", "/missing-log")

            with mock.patch.object(UI_TEST, "MAX_RUNS", 3), \
                    mock.patch.object(UI_TEST, "MAX_BYTES", 1024 * 1024):
                worker._retain()
            worker.stop()

            remaining = {path.name for path in root.iterdir() if path.is_dir()}
            self.assertEqual(len(remaining), 3)
            self.assertIn(names[-1], remaining)


class RunnerContractTest(unittest.TestCase):
    def test_tap_waits_for_active_gcode_before_advancing(self):
        callbacks = []
        reactor = type("Reactor", (), {
            "register_callback": lambda self, callback, when=None:
                callbacks.append((callback, when)),
        })()
        host = type("Host", (), {
            "reactor": reactor,
            "page": FEATHER.Page.CONTROL_MOVE,
            "command_depth": 1,
            "busy_message": "HOMING...",
        })()
        feature = UI_TEST.UITestFeature(host)
        feature.running = True
        feature.step_index = 4
        events = []
        schedules = []
        feature._event = events.append
        feature._schedule = schedules.append
        step = {"kind": "tap", "label": "move.homeall"}

        feature._after_tap(10.0, step, FEATHER.Page.CONTROL_MOVE)

        self.assertEqual(feature.step_index, 4)
        self.assertEqual(step["operation_deadline"],
                         10.0 + UI_TEST.TAP_OPERATION_TIMEOUT)
        self.assertTrue(step["expected_page_seen"])
        self.assertEqual(callbacks[0][1], 10.1)
        host.command_depth = 0
        host.busy_message = None
        # A long operation may legitimately advance from its progress page to
        # a result page before synthetic tap completion is acknowledged.
        host.page = FEATHER.Page.CALIBRATION_RESULT
        callbacks[0][0](10.1)
        self.assertEqual(feature.step_index, 5)
        self.assertEqual(events, ["PASS move.homeall"])
        self.assertEqual(schedules, [0.02])

    def test_motion_return_clamps_homing_overshoot_to_ui_limits(self):
        position = [110.0, 109.1, 220.0]
        dispatched = []
        toolhead = type("Toolhead", (), {
            "get_status": lambda self, eventtime: {
                "position": tuple(position), "homed_axes": "xyz",
            },
        })()

        def dispatch(_host, action):
            dispatched.append(action)
            position[1] = 110.0

        host = type("Host", (), {
            "reactor": type("Reactor", (), {
                "monotonic": lambda self: 10.0,
            })(),
            "toolhead": toolhead,
            "_feather_move_limits": lambda self, status: (
                (-110.0, 110.0), (-110.0, 110.0), (0.0, 220.0)),
            "_dispatch_action": dispatch,
        })()
        feature = UI_TEST.UITestFeature(host)
        feature.motion_origin = (110.0, 110.099675, 220.0)

        feature._motion_step("y", -1)

        self.assertEqual(
            dispatched, [UI_TEST.move_actions.Y_PLUS.wire_id])
        self.assertEqual(feature.motion_expected, 110.0)

    def test_motion_step_waits_for_delayed_toolhead_update(self):
        position = [110.006, 109.0, 220.0]
        dispatched = []
        toolhead = type("Toolhead", (), {
            "get_status": lambda self, eventtime: {
                "position": tuple(position), "homed_axes": "xyz",
            },
        })()
        host = type("Host", (), {
            "reactor": type("Reactor", (), {
                "monotonic": lambda self: 10.0,
            })(),
            "toolhead": toolhead,
            "_feather_move_limits": lambda self, status: (
                (-110.0, 110.0), (-110.0, 110.0), (0.0, 220.0)),
            "_dispatch_action": lambda self, action: dispatched.append(action),
        })()
        feature = UI_TEST.UITestFeature(host)
        feature.motion_origin = tuple(position)

        feature._motion_step("x", 1)

        self.assertEqual(
            dispatched, [UI_TEST.move_actions.X_MINUS.wire_id])
        self.assertFalse(feature._motion_reached("x"))
        position[0] = feature.motion_expected
        self.assertTrue(feature._motion_reached("x"))
        labels = [step["label"] for step in feature._build_steps("MOTION")]
        self.assertLess(labels.index("motion-x-forward-dispatch"),
                        labels.index("motion-x-forward-complete"))
        self.assertLess(labels.index("motion-x-forward-complete"),
                        labels.index("motion-x-forward"))

    def test_render_suite_is_nonphysical_and_waits_for_recovery(self):
        statuses = [
            {"worker_state": "running", "typer_restarts": 2},
            {"worker_state": "running", "typer_restarts": 3},
        ]
        renderer = type("Renderer", (), {
            "get_status": lambda self: statuses.pop(0) if len(statuses) > 1
            else statuses[0],
            "restart": lambda self: True,
        })()
        feature = UI_TEST.UITestFeature(type("Host", (), {
            "renderer": renderer,
        })())
        steps = feature._build_steps("RENDER")
        labels = [step["label"] for step in steps]

        self.assertEqual(labels, [
            "baseline", "render-context-start", "render-pause-timer",
            "render-restart-signal", "render-recovered",
            "render-recovered", "render-resume-timer",
            "render-context-verify"])
        steps[3]["callback"]()
        self.assertTrue(steps[4]["predicate"]())

    def test_capture_waits_for_worker_and_fails_on_dropped_batch(self):
        status = {
            "submitted_batches": 5, "rendered_batches": 4,
            "coalesced_batches": 0, "dropped_batches": 0,
        }
        renderer = type("Renderer", (), {
            "get_status": lambda self: dict(status),
            "send": lambda self, *args, **kwargs: True,
        })()
        reactor = type("Reactor", (), {"monotonic": lambda self: 10.0})()
        feature = UI_TEST.UITestFeature(type("Host", (), {
            "renderer": renderer, "reactor": reactor,
        })())
        scheduled = []
        feature._schedule = scheduled.append
        feature._screen_metadata = lambda: {"page": "IDLE_HOME"}
        captures = []
        feature.worker = type("Worker", (), {
            "capture": lambda self, *args: captures.append(args),
        })()
        step = {"kind": "capture", "label": "settled"}

        feature._capture(step)
        self.assertEqual(scheduled, [0.02])
        self.assertEqual(captures, [])

        status["dropped_batches"] = 1
        with self.assertRaisesRegex(RuntimeError, "batch dropped"):
            feature._capture(step)

    def test_capture_waits_for_presented_receipt_before_reading_frame(self):
        status = {
            "submitted_batches": 5, "rendered_batches": 5,
            "coalesced_batches": 0, "dropped_batches": 0,
        }
        submissions = []

        class Renderer:
            def get_status(self):
                return dict(status)

            def send(self, commands, **kwargs):
                submissions.append((commands, kwargs))
                status["submitted_batches"] += 1
                status["rendered_batches"] += 1
                return True

        reactor = type("Reactor", (), {"monotonic": lambda self: 10.0})()
        feature = UI_TEST.UITestFeature(type("Host", (), {
            "renderer": Renderer(), "reactor": reactor,
        })())
        feature.running = True
        feature.step_index = 3
        feature._schedule = mock.Mock()
        feature._screen_metadata = lambda: {"page": "IDLE_HOME"}
        captures = []
        feature.worker = type("Worker", (), {
            "capture": lambda self, *args: captures.append(args),
        })()
        step = {"kind": "capture", "label": "presented"}

        feature._capture(step)

        self.assertEqual(captures, [])
        self.assertEqual(len(submissions), 1)
        self.assertIn("--receipt-phase presented", submissions[0][0][0])
        token = step["capture_receipt"]
        receipt = type("Receipt", (), {
            "token": token, "success": True,
        })()
        feature.on_render_receipt(receipt, 10.0)
        feature._capture(step)

        self.assertEqual(len(captures), 1)
        self.assertNotIn(token, feature.capture_receipts)

    def test_full_suite_order_and_z_safety_sequence(self):
        renderer = type("Renderer", (), {
            "get_status": lambda self: {"typer_restarts": 0},
        })()
        feature = UI_TEST.UITestFeature(type("Host", (), {
            "renderer": renderer,
        })())
        feature.material = "PLA"
        steps = feature._build_steps("FULL")
        labels = [step["label"] for step in steps]

        first = dict((phase, next(index for index, label in enumerate(labels)
                                  if label.startswith(phase + "-")))
                     for phase in ("ui", "render", "motion", "heat", "screws",
                                   "mesh", "z"))
        self.assertEqual(list(first), [
            "ui", "render", "motion", "heat", "screws", "mesh", "z"])
        self.assertEqual(list(first.values()), sorted(first.values()))
        actions = [step.get("action") for step in steps]
        self.assertIn(UI_TEST.move_actions.HOME_ALL.wire_id, actions)
        self.assertNotIn("move.homeall", actions)
        self.assertEqual(
            actions.count(UI_TEST.z_actions.FARTHER.wire_id), 10)
        self.assertEqual(
            actions.count(UI_TEST.z_actions.CLOSER.wire_id), 10)
        self.assertIn(UI_TEST.z_actions.DISCARD_CONFIRM.wire_id, actions)
        self.assertNotIn(UI_TEST.z_actions.SAVE.wire_id, actions)
        self.assertFalse(any("pid" in label.lower() for label in labels))

    def test_ui_file_browser_returns_home_before_reopening_menu(self):
        feature = UI_TEST.UITestFeature(object())
        steps = feature._build_steps("UI")
        labels = [step["label"] for step in steps]
        returned = labels.index("ui-file-return")

        self.assertEqual(
            (steps[returned + 1]["action"], steps[returned + 1]["page"]),
            ("nav.back", FEATHER.Page.IDLE_HOME))
        self.assertEqual(
            (steps[returned + 2]["action"], steps[returned + 2]["page"]),
            ("nav.menu", FEATHER.Page.MAIN_MENU))

    def test_component_suite_discovers_declarative_pages_without_hardware(self):
        feature = UI_TEST.UITestFeature(object())

        steps = feature._build_steps("COMPONENT")
        labels = [step["label"] for step in steps]
        captures = [
            label for step, label in zip(steps, labels)
            if step["kind"] == "capture"]

        self.assertEqual(labels[0], "baseline")
        self.assertEqual(labels[1], "component-context-start")
        self.assertEqual(labels[2], "component-pause-timer")
        self.assertEqual(labels[-2], "component-resume-timer")
        self.assertEqual(labels[-1], "component-context-verify")
        self.assertEqual(len(captures), 10)
        self.assertEqual(len(set(captures)), 10)
        self.assertIn("component-default-home", captures)
        self.assertIn("component-default-render-benchmark", captures)
        self.assertTrue(all(
            label == "baseline" or label.startswith("component-default-")
            for label in captures))

    def test_component_render_uses_declared_page_title(self):
        renderer = mock.Mock()
        renderer.begin_page.return_value = ["header"]
        page = mock.Mock()
        page.title = "FORGE-X // FEATHER"
        page.page_key.value = "HOME.DASHBOARD"
        page.draw.return_value = ["body"]
        feature = UI_TEST.UITestFeature(object())
        feature.host = mock.Mock(renderer=renderer)

        feature._render_component_default(page)

        renderer.begin_page.assert_called_once_with(
            "FORGE-X // FEATHER", back=False)
        renderer.send.assert_called_once_with(["header", "body"])
        renderer.footer.assert_called_once_with(
            25.0, 0.0, 25.0, 0.0, "PREVIEW", "IDLE")

    def test_component_cases_accept_only_known_mutable_typed_state(self):
        feature = UI_TEST.UITestFeature(object())
        payload = [{
            "id": "move-fine",
            "page": "ui.pages.keys.AppPage.MOVE_STEP",
            "state": {
                "ui.pages.move.state.MoveState.JOG_STEP": 0.1,
            },
        }]
        encoded = UI_TEST.base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")).decode("ascii")

        cases = feature._decode_component_cases(encoded)

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["id"], "move-fine")
        self.assertEqual(
            [str(key) for key in cases[0]["state"]],
            ["ui.pages.move.state.MoveState.JOG_STEP"])

        payload[0]["state"] = {
            "ui.pages.move.state.MoveState.CURSOR": [10, 20],
        }
        encoded = UI_TEST.base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")).decode("ascii")
        with self.assertRaisesRegex(ValueError, "bounded declared state"):
            feature._decode_component_cases(encoded)

        payload[0].update({
            "page": "ui.pages.keys.AppPage.Z_OFFSET_PAPER",
            "state": {
                "ui.pages.z_offset.paper.state.PaperState.PROBING": True,
            },
        })
        encoded = UI_TEST.base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")).decode("ascii")
        readonly = feature._decode_component_cases(encoded)
        self.assertEqual(
            [str(key) for key in readonly[0]["state"]],
            ["ui.pages.z_offset.paper.state.PaperState.PROBING"])

    def test_printer_and_host_use_the_same_ui_fingerprint_contract(self):
        self.assertEqual(
            UI_TEST.UITestFeature._ui_fingerprint(),
            HYBRID.ui_fingerprint(ROOT))

    def test_ui_filament_back_preserves_target_before_leaving_materials(self):
        feature = UI_TEST.UITestFeature(object())
        steps = feature._build_steps("UI")
        labels = [step["label"] for step in steps]
        action_capture = next(
            index for index, step in enumerate(steps)
            if step["label"] == "ui-filament-action"
            and step["kind"] == "capture")

        self.assertEqual(labels[action_capture + 1], "ui-filament-cooling")
        self.assertEqual(labels[action_capture + 2], "ui-filament-cooling")
        self.assertEqual(labels[action_capture + 3], "ui-filament-target")
        self.assertEqual(
            (steps[action_capture + 4]["action"],
             steps[action_capture + 4]["page"]),
            ("nav.back", FEATHER.Page.FILAMENT_MATERIAL))
        self.assertEqual(
            labels[action_capture + 5], "ui-filament-target-preserved")
        self.assertEqual(labels[action_capture + 6],
                         "ui-filament-back-materials")

    def test_safe_filament_snapshot_supports_real_extruder_shape(self):
        class Extruder:
            heater = object()

            def get_status(self, eventtime):
                return {"temperature": 31.0, "target": 0.0}

        original = Extruder()
        seen = []
        host = type("Host", (), {})()
        host.heating_materials = ("PETG",)
        host.filament_material = None
        host.extruder = original
        host.page = FEATHER.Page.FILAMENT_MATERIAL
        host.reactor = type("Reactor", (), {
            "monotonic": lambda self: 1.0,
        })()

        def show(page):
            snapshot = host.extruder.get_status(0.0)
            seen.append(("show", snapshot, host.extruder.min_extrude_temp))
            host.page = page

        class FilamentFeature:
            def update(self, eventtime):
                snapshot = host.extruder.get_status(eventtime)
                seen.append((
                    "update", snapshot, host.extruder.min_extrude_temp))

        filament = FilamentFeature()
        host.feature_manager = type("FeatureManager", (), {
            "get": lambda self, name: filament,
        })()
        host._show_page = show
        feature = UI_TEST.UITestFeature(host)
        feature.material = "PETG"

        feature._render_safe_filament_action()
        feature._render_safe_filament_cooling()

        self.assertIs(host.extruder, original)
        self.assertEqual(host.filament_material, "PETG")
        self.assertEqual(seen, [
            ("show", {"temperature": 130.4, "target": 250.0}, 170.0),
            ("update", {"temperature": 260.4, "target": 250.0}, 170.0),
        ])

    def test_hardware_calibration_open_resets_ui_catalog_page(self):
        calibration = type("Calibration", (), {"calibration_page": 2})()
        manager = type("Manager", (), {
            "get": lambda self, name: calibration,
        })()
        host = type("Host", (), {
            "feature_manager": manager,
            "page": FEATHER.Page.IDLE_HOME,
            "_show_page": lambda self, page: setattr(self, "page", page),
        })()
        feature = UI_TEST.UITestFeature(host)

        feature._open_calibration_home()

        self.assertEqual(calibration.calibration_page, 0)
        self.assertEqual(host.page, FEATHER.Page.CALIBRATION_HOME)

    def test_test_mode_blocks_only_persistent_actions(self):
        feature = UI_TEST.UITestFeature(object())
        self.assertFalse(feature.blocks_action("z.save"))
        feature.running = True
        self.assertTrue(feature.input_blocked)
        self.assertTrue(feature.blocks_action("z.save"))
        self.assertTrue(feature.blocks_action(
            UI_TEST.z_actions.SAVE.wire_id))
        self.assertTrue(feature.blocks_action(
            UI_TEST.z_actions.SAFE_SAVE.wire_id))
        self.assertTrue(feature.blocks_action("cal.mesh.save"))
        self.assertFalse(feature.blocks_action("z.farther"))

    def test_controller_drops_persistent_action_while_runner_is_active(self):
        controller = FEATHER.FeatherScreen.__new__(FEATHER.FeatherScreen)
        controller.print_state = FEATHER.PrintState.IDLE
        controller.command_depth = 0
        controller.busy_message = None
        controller.feature_manager = LazyFeatureManager(
            controller, FEATHER.FEATURE_SPECS)
        feature = controller.feature_manager.get("ui_test")
        feature.running = True

        # Returning before page routing is the contract: no save handler or
        # additional controller fixture is needed for this safety check.
        controller._dispatch_action("cal.mesh.save")

    def test_test_action_propagates_controller_failure_after_message(self):
        class FailingOwner:
            def allows_action(self, page, action):
                return True

            def handle_action(self, page, action):
                raise RuntimeError("synthetic command failed")

        class Manager:
            def __init__(self, feature):
                self.feature = feature
                self.owner = FailingOwner()

            def peek(self, name):
                return self.feature if name == "ui_test" else None

            def owner_name(self, page):
                return "failing"

            def resolve_semantic_action(self, page, action):
                return self.owner, None

            def handle_immediate_action(self, page, action):
                return False

            @property
            def input_blocked(self):
                return False

        controller = FEATHER.FeatherScreen.__new__(FEATHER.FeatherScreen)
        controller.print_state = FEATHER.PrintState.IDLE
        controller.command_depth = 0
        controller.busy_message = None
        controller.page = FEATHER.Page.IDLE_HOME
        controller.previous_page = FEATHER.Page.IDLE_HOME
        controller.last_action_time = -1.0
        controller.pending_action = None
        controller.reactor = type("Reactor", (), {
            "monotonic": lambda self: 1.0,
        })()
        feature = UI_TEST.UITestFeature(controller)
        controller.feature_manager = Manager(feature)
        messages = []
        controller._show_message = lambda message, page: messages.append(
            (message, page))

        controller._dispatch_action("synthetic.action")
        self.assertEqual(messages, [("synthetic command failed",
                                     FEATHER.Page.IDLE_HOME)])

        feature.dispatching_test_action = True
        controller.last_action_time = -1.0
        with self.assertRaisesRegex(RuntimeError, "synthetic command failed"):
            controller._handle_touch_action("synthetic.action")
        self.assertEqual(len(messages), 2)

    def test_nonphysical_cleanup_does_not_issue_hardware_gcode(self):
        shown = []
        host = type("Host", (), {
            "timer": None,
            "filament_material": "PETG",
            "previous_page": FEATHER.Page.MAIN_MENU,
            "_run_script": lambda self, command: (_ for _ in ()).throw(
                AssertionError("UI suite issued hardware G-code: %s" % command)),
            "_show_page": lambda self, page: shown.append(page),
        })()
        for suite in ("UI", "COMPONENT", "RENDER"):
            feature = UI_TEST.UITestFeature(host)
            feature.suite = suite
            feature.original = {
                "filament_material": "PLA", "timer_active": False,
                "page": FEATHER.Page.IDLE_HOME,
                "previous_page": FEATHER.Page.CONTROL_HOME,
            }
            feature._restore_state()

        self.assertEqual(host.filament_material, "PLA")
        self.assertEqual(shown, [FEATHER.Page.IDLE_HOME] * 3)
        self.assertEqual(host.previous_page, FEATHER.Page.CONTROL_HOME)

    def test_status_does_not_load_lazy_feature(self):
        controller = FEATHER.FeatherScreen.__new__(FEATHER.FeatherScreen)
        controller.feature_manager = LazyFeatureManager(
            controller, FEATHER.FEATURE_SPECS)
        gcmd = GCmd({"ACTION": "STATUS"})

        controller.cmd_FEATHER_UI_TEST(gcmd)

        self.assertIsNone(controller.feature_manager.peek("ui_test"))
        self.assertEqual(
            gcmd.responses, ["Feather UI test: idle (feature not loaded)"])

    def test_run_is_the_only_command_path_that_loads_feature(self):
        controller = FEATHER.FeatherScreen.__new__(FEATHER.FeatherScreen)
        controller.feature_manager = LazyFeatureManager(
            controller, FEATHER.FEATURE_SPECS)
        gcmd = GCmd({"ACTION": "RUN", "SUITE": "UI", "CONFIRM": 1})
        calls = []

        with mock.patch.object(
                UI_TEST.UITestFeature, "run",
                lambda feature, *args: calls.append((feature, args))):
            controller.cmd_FEATHER_UI_TEST(gcmd)

        feature = controller.feature_manager.peek("ui_test")
        self.assertIsNotNone(feature)
        self.assertEqual(calls, [(feature, (gcmd, "UI", "", 1, ""))])

    def test_stale_same_process_run_is_cleaned_before_next_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            previous = root / "old"
            previous.mkdir()
            active = root / "active.json"
            active.write_text(json.dumps({
                "pid": 42, "runtime_z": 0.125, "mesh_profile": "auto",
                "directory": str(previous), "session": "42-test",
                "hardware": True,
            }), encoding="utf-8")
            commands = []
            host = type("Host", (), {
                "_run_script": lambda self, command: commands.append(command),
            })()
            feature = UI_TEST.UITestFeature(host)
            feature.session_id = "42-test"

            with mock.patch.object(UI_TEST, "ACTIVE_MARKER", str(active)), \
                    mock.patch.object(UI_TEST.os, "getpid", return_value=42):
                feature._recover_stale_marker()

            self.assertEqual(commands, [
                "TURN_OFF_HEATERS",
                "_SET_GCODE_OFFSET Z=+0.125000 MOVE=0",
                "BED_MESH_PROFILE LOAD=auto",
                "M84",
            ])
            summary = json.loads((previous / "summary.json").read_text())
            self.assertEqual(summary["outcome"], "interrupted")
            self.assertFalse(active.exists())

    def test_stale_different_session_does_not_reapply_volatile_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            active = root / "active.json"
            active.write_text(json.dumps({
                "session": "old", "hardware": True,
                "runtime_z": 1.5, "mesh_profile": "auto",
            }), encoding="utf-8")
            commands = []
            host = type("Host", (), {
                "_run_script": lambda self, command: commands.append(command),
            })()
            feature = UI_TEST.UITestFeature(host)
            feature.session_id = "new"

            with mock.patch.object(UI_TEST, "ACTIVE_MARKER", str(active)):
                feature._recover_stale_marker()

            self.assertEqual(commands, [])
            self.assertFalse(active.exists())

    def test_extended_context_suites_are_separate_and_ordered(self):
        host = type("Host", (), {
            "cold_pull_materials": ("PLA",),
            "renderer": type("Renderer", (), {
                "get_status": lambda self: {"typer_restarts": 0},
            })(),
        })()
        feature = UI_TEST.UITestFeature(host)
        feature.material = "PLA"
        material = feature._build_steps("CONTEXT_MATERIAL")
        material_labels = [step["label"] for step in material]
        self.assertLess(material_labels.index("filament-context-start"),
                        material_labels.index("prompt-PLA"))
        self.assertLess(material_labels.index("prompt-Load"),
                        material_labels.index("prompt-Purge"))
        self.assertLess(material_labels.index("prompt-Purge"),
                        material_labels.index("prompt-Unload"))
        self.assertLess(material_labels.index("prompt-Unload"),
                        material_labels.index("prompt-Done"))
        self.assertLess(material_labels.index("filament-context-verify"),
                        material_labels.index("cold_pull-context-start"))

        printing = feature._build_steps("CONTEXT_PRINT")
        print_labels = [step["label"] for step in printing]
        expected = (
            "print_kamp-context-start", "print_kamp-file-open",
            "print_kamp-complete", "print_kamp-context-verify",
            "print_mesh-context-start", "print_mesh-idle-timeout",
            "print_mesh-checkpoint", "print_mesh-activate-recovery",
            "print_mesh-context-verify", "recovery-context-start",
            "recovery-printing", "recovery-complete",
            "recovery-context-verify",
        )
        positions = [print_labels.index(label) for label in expected]
        self.assertEqual(positions, sorted(positions))

        full_labels = [
            step["label"] for step in feature._build_steps("FULL")]
        self.assertFalse(any(label.startswith("context_print-")
                             for label in full_labels))
        self.assertFalse(any(label.startswith("context_material-")
                             for label in full_labels))

    def test_action_prompt_selection_uses_exact_visible_label(self):
        host = type("Host", (), {})()
        host.action_prompt = {"buttons": {
            "prompt.button.0": {"label": "Load"},
            "prompt.button.1": {"label": "Unload"},
        }}
        feature = UI_TEST.UITestFeature(host)

        self.assertEqual(feature._prompt_action_for_label("load"),
                         "prompt.button.0")
        with self.assertRaisesRegex(RuntimeError, "found 0"):
            feature._prompt_action_for_label("Purge")

    def test_context_file_is_published_through_real_browser_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "runner.gcode"
            path.write_text("G4 P1\n", encoding="utf-8")
            host = type("Host", (), {})()
            host.virtual_sdcard = type("SD", (), {
                "sdcard_dirname": temporary,
            })()
            host.reactor = type("Reactor", (), {
                "monotonic": lambda self: 12.0,
            })()
            host.file_entry_cache = {"internal": ["original"]}
            host.file_entry_loaded_at = {"internal": 4.0}
            host.file_entries = ["original"]
            host.file_source = "internal"
            host.file_page = 2
            host.file_scan_token = 3
            host.page = FEATHER.Page.IDLE_HOME
            host._show_page = lambda page: setattr(host, "page", page)
            feature = UI_TEST.UITestFeature(host)

            feature._open_context_file(str(path))

            entry = host.file_entry_cache["internal"][0]
            self.assertEqual(entry["path"], str(path.resolve()))
            self.assertEqual(host.file_entries, [entry])
            self.assertEqual(host.page, FEATHER.Page.FILE_BROWSER)
            self.assertEqual(host.file_scan_token, 4)
            self.assertEqual(
                feature.context_runtime["file_browser"]["cache"],
                ["original"])

    def test_extended_suites_require_confirm_two(self):
        feature = UI_TEST.UITestFeature(object())
        gcmd = GCmd()
        for suite in ("CONTEXT_PRINT", "CONTEXT_MATERIAL"):
            with self.subTest(suite=suite), self.assertRaisesRegex(
                    RuntimeError, "requires CONFIRM=2"):
                feature.run(gcmd, suite, "PLA", 1)

    def test_context_print_preflight_rejects_foreign_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = pathlib.Path(temporary) / "resurrection.json"
            checkpoint.write_text("{}", encoding="utf-8")
            empty_context = type("Context", (), {
                "get_status": lambda self, eventtime: {"contexts": ()},
            })()
            host = type("Host", (), {})()
            host.reactor = type("Reactor", (), {
                "monotonic": lambda self: 1.0,
            })()
            host.print_state = FEATHER.PrintState.IDLE
            host.print_stats = type("Stats", (), {
                "get_status": lambda self, eventtime: {"state": "standby"},
            })()
            host.virtual_sdcard = type("SD", (), {
                "is_active": lambda self: False,
            })()
            host.command_depth = 0
            host.renderer = type("Renderer", (), {"active": True})()
            host.toolhead = type("Toolhead", (), {
                "get_status": lambda self, eventtime: {"velocity": 0.0},
            })()
            host.operation_context = empty_context
            host.bed_mesh = type("Mesh", (), {
                "get_status": lambda self, eventtime: {
                    "profiles": ("auto",)},
            })()
            host.probe = object()
            heater = type("Heater", (), {
                "get_status": lambda self, eventtime: {"target": 0.0},
            })()
            host.extruder = heater
            host.heater_bed = heater
            host.heating_materials = ("PLA",)
            host.resurrection = type("Resurrection", (), {
                "enabled": True, "file_path": str(checkpoint),
            })()
            feature = UI_TEST.UITestFeature(host)

            with self.assertRaisesRegex(
                    RuntimeError, "foreign recovery checkpoint"):
                feature._preflight("CONTEXT_PRINT", hardware_targets=True)

    def test_context_runtime_cleanup_restores_memory_and_owned_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            gcode = root / "runner.gcode"
            checkpoint = root / "checkpoint.json"
            unrelated = root / "keep.gcode"
            for path in (gcode, checkpoint, unrelated):
                path.write_text("test", encoding="utf-8")

            class Params:
                def __init__(self):
                    self.variables = {
                        "check_md5": 0, "current_material": "PLA"}

                def _store_value(self, param, value):
                    return "product"

            params = Params()
            original_store = params._store_value
            params._store_value = lambda param, value: "guard"
            client = type("Client", (), {
                "variables": {"idle_timeout": 2},
            })()
            commands = []
            host = type("Host", (), {})()
            host.reactor = type("Reactor", (), {
                "monotonic": lambda self: 1.0,
            })()
            host.virtual_sdcard = type("SD", (), {
                "is_active": lambda self: False,
            })()
            host.operation_context = type("Context", (), {
                "get_status": lambda self, eventtime: {"contexts": ()},
            })()
            host.params = params
            host.idle_timeout = object()
            host.resurrection = type("Resurrection", (), {
                "file_path": str(checkpoint),
            })()
            host._run_script = lambda command: commands.append(command)
            feature = UI_TEST.UITestFeature(host)
            feature.suite = "CONTEXT_PRINT"
            feature.context_files = [str(gcode)]
            feature.context_checkpoint = str(checkpoint)
            feature.context_runtime = {
                "mod_params": {"check_md5": 1},
                "current_material": "PETG",
                "params_store": (params, True, original_store),
                "client_macro": client,
                "client_idle_timeout": 3600,
                "idle_timeout": 600.0,
            }

            feature._restore_context_runtime()

            self.assertEqual(params.variables["check_md5"], 1)
            self.assertEqual(params.variables["current_material"], "PETG")
            self.assertIs(params._store_value, original_store)
            self.assertEqual(client.variables["idle_timeout"], 3600)
            self.assertIn("SET_IDLE_TIMEOUT TIMEOUT=600", commands)
            self.assertFalse(gcode.exists())
            self.assertFalse(checkpoint.exists())
            self.assertTrue(unrelated.exists())


class ContextManagerFixture:
    def __init__(self):
        self.status = self._raw({
            "contexts": [], "context_path": [], "current_state": None,
            "cancel_available": False, "cancel_pending": False,
            "cancel_target": {"type": None, "name": None, "mode": None},
            "cancel_blocker": {"type": None, "name": None},
        })
        self.original_calls = 0

    def _changed(self):
        self.original_calls += 1

    def get_status(self, eventtime):
        return dict(self.status)

    @staticmethod
    def _raw(snapshot):
        contexts = []
        for index, frame in enumerate(snapshot["contexts"], 100):
            value = dict(frame)
            value["id"] = index
            contexts.append(value)
        target = snapshot["cancel_target"]
        blocker = snapshot["cancel_blocker"]
        return {
            "contexts": contexts,
            "context_path": tuple(snapshot["context_path"]),
            "current_state": snapshot["current_state"],
            "cancel_available": snapshot["cancel_available"],
            "cancel_pending": snapshot["cancel_pending"],
            "cancel_request_id": 700,
            "cancel_target_id": 101 if target["type"] else None,
            "cancel_target_type": target["type"],
            "cancel_target_name": target["name"],
            "cancel_target_mode": target["mode"],
            "cancel_blocker_id": 102 if blocker["type"] else None,
            "cancel_blocker_type": blocker["type"],
            "cancel_blocker_name": blocker["name"],
            "revision": 999,
        }

    def emit(self, snapshot):
        self.status = self._raw(snapshot)
        self._changed()


class OperationContextRecorderTest(unittest.TestCase):
    def _record(self, fixture, variants):
        manager = ContextManagerFixture()
        recorder = CONTEXT_FIXTURES.OperationContextRecorder(manager)
        recorder.attach()
        recorder.start_scenario("fixture", (fixture,))
        expected = CONTEXT_FIXTURES.expand_events(
            CONTEXT_FIXTURES.FIXTURES[fixture], variants)
        for snapshot in expected:
            manager.emit(snapshot)
        result = recorder.finish_scenario()
        recorder.detach()
        return manager, recorder, result

    def test_records_nested_stack_and_ignores_dynamic_fields(self):
        manager, recorder, result = self._record(
            "mesh_clean", ("NONE", "HEATING", "COOLING"))

        self.assertTrue(result["passed"])
        self.assertEqual(result["temperature_variants"],
                         ["NONE", "HEATING", "COOLING"])
        nested = next(snapshot for snapshot in result["actual"]
                      if len(snapshot["contexts"]) == 3)
        self.assertEqual([frame["type"] for frame in nested["contexts"]],
                         ["auto_bed_level", "bed_level", "nozzle_clean"])
        self.assertEqual(manager.original_calls, len(result["actual"]))
        self.assertNotIn("_changed", manager.__dict__)
        self.assertTrue(recorder.report()["passed"])

    def test_all_temperature_boundary_variants_are_exact(self):
        self.assertEqual(
            CONTEXT_FIXTURES.temperature_variant(199.9, 200, 205),
            "HEATING")
        self.assertEqual(
            CONTEXT_FIXTURES.temperature_variant(205.1, 200, 205),
            "COOLING")
        self.assertEqual(
            CONTEXT_FIXTURES.temperature_variant(200, 200, 205), "NONE")
        self.assertEqual(
            CONTEXT_FIXTURES.temperature_variant(205, 200, 205), "NONE")
        for variant in CONTEXT_FIXTURES.WAIT_VARIANTS:
            with self.subTest(variant=variant):
                _manager, _recorder, result = self._record(
                    "screws", (variant,))
                self.assertEqual(result["temperature_variants"], [variant])

    def test_mismatch_reports_first_exact_difference_and_final_stack(self):
        manager = ContextManagerFixture()
        recorder = CONTEXT_FIXTURES.OperationContextRecorder(manager)
        recorder.attach()
        recorder.start_scenario("screws", ("screws",))
        events = CONTEXT_FIXTURES.expand_events(
            CONTEXT_FIXTURES.SCREWS, ("NONE",))
        for snapshot in events[:-1]:
            manager.emit(snapshot)

        with self.assertRaisesRegex(
                CONTEXT_FIXTURES.FixtureMismatch,
                "final operation stack is not empty"):
            recorder.finish_scenario()
        self.assertFalse(recorder.report()["passed"])
        recorder.detach()

    def test_wrapper_restores_external_instance_state_on_exception_and_abort(self):
        manager = ContextManagerFixture()
        calls = []

        def external_changed():
            calls.append("external")
            raise RuntimeError("original failed")

        manager._changed = external_changed
        recorder = CONTEXT_FIXTURES.OperationContextRecorder(manager)
        recorder.attach()
        recorder.start_scenario("none", ("none",))
        with self.assertRaisesRegex(RuntimeError, "original failed"):
            manager._changed()
        self.assertEqual(len(recorder._trace), 1)
        recorder.abort_active("aborted")
        recorder.detach()

        self.assertIs(manager._changed, external_changed)
        self.assertEqual(calls, ["external"])
        self.assertEqual(recorder.results[0]["diagnostic"], "aborted")

    def test_transition_between_scenarios_is_a_fail_closed_leak(self):
        manager = ContextManagerFixture()
        recorder = CONTEXT_FIXTURES.OperationContextRecorder(manager)
        recorder.attach()
        manager.emit(CONTEXT_FIXTURES.expand_events(
            CONTEXT_FIXTURES.SCREWS, ("NONE",))[-1])

        with self.assertRaisesRegex(
                CONTEXT_FIXTURES.FixtureMismatch,
                "transitions occurred between scenarios"):
            recorder.start_scenario("next", ("none",))

        self.assertFalse(recorder.results[0]["passed"])
        recorder.detach()

    def test_feature_deactivation_restores_changed_wrapper(self):
        manager = ContextManagerFixture()
        feature = UI_TEST.UITestFeature(type("Host", (), {})())
        feature.context_recorder = CONTEXT_FIXTURES.OperationContextRecorder(
            manager)
        feature.context_recorder.attach()
        feature.context_recorder.start_scenario("none", ("none",))

        feature.deactivate()

        self.assertFalse(feature.context_recorder.attached)
        self.assertNotIn("_changed", manager.__dict__)
        self.assertEqual(
            feature.context_recorder.results[0]["diagnostic"],
            "feature deactivated")


if __name__ == "__main__":
    unittest.main()
