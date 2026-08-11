## On-printer regression run lifecycle for the Feather display.
##
## This module is deliberately absent from Feather's normal import graph.  It
## is loaded only by the hidden _FEATHER_UI_TEST command.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import hashlib
import json
import logging
import os
import shutil
import time
from datetime import datetime

from ui import Page, PrintState
from ff5m_ui.z_offset import actions as z_actions
from .artifacts import (
    ACTIVE_MARKER, ARTIFACT_ROOT, ArtifactWorker,
    SCREEN_HEIGHT, SCREEN_WIDTH, _atomic_json, _jsonable,
)
from .context_fixtures import OperationContextRecorder
from .scenarios import ScenarioCatalog
from .resources import (
    ContextTestFixture, PrinterStateSnapshot,
    recover_interrupted_context_resources,
)


CAPTURE_RECEIPT_TIMEOUT = 6.0
TAP_OPERATION_TIMEOUT = 1900.0
PERSISTENT_ACTIONS = frozenset((
    "cal.mesh.save", "cal.tuning.save", "z.save", "live_z.save",
    "live_z.save.yes", "mod.apply", "mod.save", "error.restart",
    "error.firmware_restart", z_actions.SAVE.wire_id,
    z_actions.SAFE_SAVE.wire_id,
))
VALID_SUITES = frozenset((
    "FULL", "UI", "COMPONENT", "RENDER", "MOTION", "HEAT", "SCREWS",
    "MESH", "Z", "CONTEXT_PRINT", "CONTEXT_MATERIAL",
))
NONPHYSICAL_SUITES = frozenset(("UI", "COMPONENT", "RENDER"))
EXTENDED_CONTEXT_SUITES = frozenset(("CONTEXT_PRINT", "CONTEXT_MATERIAL"))
UI_FINGERPRINT_FILES = (
    "feather_screen.py",
    "feather_feature_ui_test.py",
    "feather_operation_context_fixtures.py",
)
UI_FINGERPRINT_PACKAGES = ("ui", "ff5m_ui", "feather_ui_test")


class UITestRun:
    """Own all mutable state and callbacks for one requested test run."""

    def __init__(self, host, session_id=None, on_finished=None):
        self.host = host
        self.session_id = (session_id or
                           "%d-%.6f" % (os.getpid(), time.time()))
        self.on_finished = on_finished
        self.running = False
        self.abort_requested = False
        self.suite = None
        self.material = None
        self.phase = "idle"
        self.steps = []
        self.step_index = 0
        self.step_runtime = {}
        self.gcmd = None
        self.run_id = None
        self.run_directory = None
        self.worker = None
        self.capture_number = 0
        self.failures = []
        self.started_at = None
        self.snapshot = None
        self.finalizing = False
        self._last_stage_status = None
        self.calibration_stages = []
        self.test_results = {}
        self.renderer_dropped = 0
        self.capture_receipts = {}
        self.context_recorder = None
        self.context_fixture = None
        self.scenarios = ScenarioCatalog(self)

    @property
    def reactor(self):
        return self.host.reactor

    @property
    def input_blocked(self):
        # Immediate emergency actions are checked before this product lock.
        return self.running and not self.finalizing

    @property
    def theme_update_blocked(self):
        return self.running and not self.finalizing

    def blocks_action(self, action):
        return (self.running and not self.finalizing
                and str(action) in PERSISTENT_ACTIONS)

    def handle_immediate_action(self, page, action):
        return False

    def update(self, eventtime):
        pass

    def on_print_status(self, status):
        status = str(status)
        if (not self.running or self.finalizing
                or status == self._last_stage_status
                or self.phase not in ("screws", "mesh", "z")):
            return
        self._last_stage_status = status
        self.calibration_stages.append({
            "time": time.time(), "phase": self.phase, "status": status,
        })
        self.capture_number += 1
        number = self.capture_number
        label = "%s-stage-%s" % (self.phase, status)
        try:
            metadata = self._screen_metadata()
        except Exception:
            logging.exception("[feather_ui_test] unable to snapshot stage")
            return
        self.worker.capture(number, label, metadata,
                            self._stage_capture_finished)

    def _stage_capture_finished(self, result):
        if isinstance(result, Exception):
            self._event("stage capture failed: %s" % result)

    def on_gcode_output(self, message):
        pass

    def on_render_receipt(self, receipt, eventtime):
        token = str(getattr(receipt, "token", ""))
        if self.running and token.startswith("ui-test:"):
            self.capture_receipts[token] = receipt

    def on_print_state_changed(self, old_state, new_state, stats_state):
        if (self.running and self.suite != "CONTEXT_PRINT"
                and new_state != PrintState.IDLE):
            self.abort_requested = True

    def safety_active_reasons(self, eventtime):
        return (("ui-test",) if self.running and not self.finalizing
                and self.phase not in ("ui", "component") else ())

    def safety_armed_reasons(self, page, eventtime):
        return ()

    def respond_status(self, gcmd):
        if not self.running:
            gcmd.respond_info("Feather UI test: idle")
            return
        gcmd.respond_info(
            "Feather UI test: suite=%s phase=%s step=%d/%d artifacts=%s" %
            (self.suite, self.phase, self.step_index, len(self.steps),
             self.run_directory))

    def abort(self, gcmd):
        if not self.running:
            gcmd.respond_info("Feather UI test: nothing to abort")
            return
        self.abort_requested = True
        if self._temperature_wait_active():
            try:
                self.host._run_immediate_command("M108")
            except Exception:
                logging.exception(
                    "[feather_ui_test] unable to interrupt temperature wait")
        gcmd.respond_info("Feather UI test abort requested: %s" % self.run_id)
        self._schedule(0.0)

    def run(self, gcmd, suite, material, confirm, encoded_cases=""):
        if self.running:
            raise gcmd.error("Feather UI test is already running")
        if suite not in VALID_SUITES:
            raise gcmd.error("Unknown Feather UI test SUITE=%s" % suite)
        required_confirm = 2 if suite in EXTENDED_CONTEXT_SUITES else 1
        if confirm != required_confirm:
            raise gcmd.error(
                "Feather UI test SUITE=%s requires CONFIRM=%d" % (
                    suite, required_confirm))
        if encoded_cases and suite != "COMPONENT":
            raise gcmd.error("CASES is supported only by SUITE=COMPONENT")
        self._preflight(suite, hardware_targets=False)
        self.suite = suite
        self.component_cases = self.scenarios._decode_component_cases(
            encoded_cases) if suite == "COMPONENT" else ()
        self.material = self._resolve_material(material, suite)
        self.gcmd = gcmd
        self.started_at = time.time()
        self.abort_requested = False
        self.failures = []
        self.step_index = 0
        self.step_runtime = {}
        self.capture_number = 0
        self.calibration_stages = []
        self.test_results = {}
        self.capture_receipts = {}
        self.renderer_dropped = self.host.renderer.get_status()[
            "dropped_batches"]
        self._capture_original_state()
        self._recover_stale_marker()
        # Recovery may have restored a profile or runtime offset belonging to
        # the interrupted run.  That recovered state is the baseline the new
        # run must preserve, not the transient state observed before cleanup.
        self._capture_original_state()
        self._preflight(suite, hardware_targets=True)
        self.run_id = "%s-%s" % (
            datetime.now().strftime("%Y%m%d-%H%M%S-%f"), suite.lower())
        self.context_fixture = ContextTestFixture(
            self.host, self.reactor, self.run_id, self.material,
            changed=self._persist_resource_marker)
        self.steps = self.scenarios.build_steps(suite)
        directory_created = False
        try:
            os.makedirs(ARTIFACT_ROOT, exist_ok=True)
            if shutil.disk_usage(ARTIFACT_ROOT).free < 64 * 1024 * 1024:
                raise gcmd.error("Less than 64 MiB is free below /data")
            self.run_directory = os.path.join(ARTIFACT_ROOT, self.run_id)
            os.makedirs(self.run_directory)
            directory_created = True
            _atomic_json(ACTIVE_MARKER, self._marker("starting"))
            _atomic_json(
                os.path.join(self.run_directory, "environment.json"),
                self._environment())
            self.worker = ArtifactWorker(self.reactor, self.run_directory)
            self._attach_context_recorder()
            self.running = True
            self.finalizing = False
            self._last_stage_status = None
            self._event("run started suite=%s material=%s" %
                        (suite, self.material or "n/a"))
            gcmd.respond_info(
                "Feather UI test started: suite=%s artifacts=%s" %
                (suite, self.run_directory))
            self._schedule(0.0)
        except Exception:
            self.running = False
            self._detach_context_recorder()
            if self.worker is not None:
                self.worker.stop()
                self.worker = None
            self._discard_active_marker()
            if directory_created:
                try:
                    shutil.rmtree(self.run_directory)
                except OSError:
                    logging.exception(
                        "[feather_ui_test] unable to remove failed run setup")
            self.run_directory = None
            raise

    def _persist_resource_marker(self):
        if self.run_directory is not None and self.running:
            _atomic_json(ACTIVE_MARKER, self._marker(self.phase))

    def _discard_active_marker(self):
        try:
            with open(ACTIVE_MARKER, "r", encoding="utf-8") as stream:
                marker = json.load(stream)
            if (marker.get("run_id") != self.run_id
                    or marker.get("session") != self.session_id):
                return
            os.unlink(ACTIVE_MARKER)
        except (OSError, ValueError):
            pass

    def _preflight(self, suite, hardware_targets=True):
        if self.host.print_state != PrintState.IDLE:
            raise RuntimeError("Printer must be idle")
        state = str(self.host.print_stats.get_status(
            self.reactor.monotonic()).get("state", "")).lower()
        if state in ("printing", "paused"):
            raise RuntimeError("A print is active")
        if self.host.virtual_sdcard.is_active():
            raise RuntimeError("Virtual SD is active")
        if getattr(self.host, "command_depth", 0):
            raise RuntimeError("Another Feather command is active")
        if not getattr(self.host.renderer, "active", False):
            raise RuntimeError("Feather renderer is not active")
        status = self.host.toolhead.get_status(self.reactor.monotonic())
        if abs(float(status.get("velocity", 0.0) or 0.0)) > 0.0001:
            raise RuntimeError("Toolhead is moving")
        if hardware_targets and suite not in NONPHYSICAL_SUITES:
            operation_context = getattr(self.host, "operation_context", None)
            if operation_context is None:
                raise RuntimeError("Operation context is not configured")
            if operation_context.get_status(
                    self.reactor.monotonic()).get("contexts"):
                raise RuntimeError("Operation context stack is not empty")
            if suite in ("FULL", "MESH", "CONTEXT_PRINT",
                         "CONTEXT_MATERIAL") and getattr(
                    self.host, "bed_mesh", None) is None:
                raise RuntimeError("Bed mesh is not configured")
            if suite in ("FULL", "SCREWS", "MESH", "Z",
                         "CONTEXT_PRINT", "CONTEXT_MATERIAL") and getattr(
                    self.host, "probe", None) is None:
                raise RuntimeError("Probe is not configured")
            extruder = self.host.extruder.get_status(self.reactor.monotonic())
            bed = self.host.heater_bed.get_status(self.reactor.monotonic())
            if float(extruder.get("target", 0.0)) > 0.0 or float(
                    bed.get("target", 0.0)) > 0.0:
                raise RuntimeError("Turn heaters off before hardware tests")
            if suite in EXTENDED_CONTEXT_SUITES:
                if not getattr(self.host, "heating_materials", ()):
                    raise RuntimeError("No heating material profiles are available")
                if suite == "CONTEXT_MATERIAL" and not getattr(
                        self.host, "cold_pull_materials", ()):
                    raise RuntimeError("No cold-pull material profiles are available")
                resurrection = getattr(self.host, "resurrection", None)
                if (resurrection is not None
                        and os.path.isfile(getattr(
                            resurrection, "file_path", ""))):
                    raise RuntimeError(
                        "A foreign recovery checkpoint already exists")
                if suite == "CONTEXT_PRINT":
                    profiles = self.host.bed_mesh.get_status(
                        self.reactor.monotonic()).get("profiles", ())
                    if "auto" not in profiles:
                        raise RuntimeError(
                            "Bed mesh profile 'auto' is required")
                    if resurrection is None or not getattr(
                            resurrection, "enabled", False):
                        raise RuntimeError("Print recovery is not enabled")

    def _resolve_material(self, requested, suite):
        needs_material = suite in (
            "FULL", "HEAT", "MESH", "CONTEXT_PRINT", "CONTEXT_MATERIAL")
        if not needs_material:
            return requested or None
        materials = tuple(self.host.heating_materials)
        if not materials:
            raise RuntimeError("No heating materials are enabled")
        material = requested.upper() if requested else self.host._current_material()
        if material not in materials:
            material = materials[0] if not requested else material
        if material not in materials:
            raise RuntimeError("Unknown or inactive heating material: %s" % material)
        if suite == "CONTEXT_MATERIAL":
            cold_pull = tuple(self.host.cold_pull_materials)
            if requested and material not in cold_pull:
                raise RuntimeError(
                    "Material has no active cold-pull profile: %s" % material)
            if material not in cold_pull:
                shared = [name for name in materials if name in cold_pull]
                if not shared:
                    raise RuntimeError(
                        "Heating and cold-pull profiles have no common material")
                material = shared[0]
        return material

    def _capture_original_state(self):
        self.snapshot = PrinterStateSnapshot.capture(
            self.host, self.reactor)

    def _marker(self, phase):
        return {
            "run_id": self.run_id, "pid": os.getpid(), "suite": self.suite,
            "session": self.session_id,
            "hardware": self.suite not in NONPHYSICAL_SUITES,
            "phase": phase,
            "runtime_z": (0.0 if self.snapshot is None
                          else self.snapshot.runtime_z),
            "mesh_profile": ("" if self.snapshot is None
                             else self.snapshot.mesh_profile),
            "directory": self.run_directory,
            "resources": ({} if self.context_fixture is None
                          else self.context_fixture.marker_state()),
            "updated_at": time.time(),
        }

    def _recover_stale_marker(self):
        try:
            with open(ACTIVE_MARKER, "r", encoding="utf-8") as stream:
                marker = json.load(stream)
        except (OSError, ValueError):
            return
        # A Klipper restart already cleared volatile heaters, motion, mesh and
        # runtime offsets.  Same-process recovery restores the recorded state.
        if (marker.get("session") == self.session_id
                and marker.get("hardware", True)):
            self.host._run_script("TURN_OFF_HEATERS")
            self.host._run_script("_SET_GCODE_OFFSET Z=%+.6f MOVE=0" %
                                  float(marker.get("runtime_z", 0.0)))
            profile = str(marker.get("mesh_profile", "") or "")
            self.host._run_script(
                "BED_MESH_PROFILE LOAD=%s" % profile
                if profile else "BED_MESH_CLEAR")
            self.host._run_script("M84")
        recover_interrupted_context_resources(self.host, marker)
        directory = marker.get("directory")
        if directory and os.path.isdir(directory):
            try:
                _atomic_json(os.path.join(directory, "summary.json"), {
                    "outcome": "interrupted", "recovered_at": time.time(),
                    "reason": "superseded by a new on-printer run",
                })
            except OSError:
                logging.exception(
                    "[feather_ui_test] unable to annotate stale run")
        try:
            os.unlink(ACTIVE_MARKER)
        except OSError:
            pass

    def _environment(self):
        now = self.reactor.monotonic()
        start_args = getattr(self.host.printer, "get_start_args", lambda: {})()
        return {
            "run_id": self.run_id, "suite": self.suite,
            "material": self.material, "pid": os.getpid(),
            "software_version": start_args.get("software_version"),
            "ui_fingerprint": self._ui_fingerprint(),
            "theme": getattr(self.host.renderer, "theme_name", None),
            "page": getattr(self.host.page, "name", str(self.host.page)),
            "heating_materials": self.host.heating_materials,
            "heating_profiles": self.host.heating_profiles,
            "current_material": self.host._current_material(),
            "print": self.host.print_stats.get_status(now),
            "toolhead": self.host.toolhead.get_status(now),
            "renderer": self.host.renderer.get_status(),
        }

    @staticmethod
    def _ui_fingerprint():
        """Hash the deployed UI/framework sources only when a test starts."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        files = []
        for relative in UI_FINGERPRINT_FILES:
            path = os.path.join(root, relative)
            if os.path.isfile(path):
                files.append((relative, path))
        for package in UI_FINGERPRINT_PACKAGES:
            package_root = os.path.join(root, package)
            for current, directories, names in os.walk(package_root):
                directories[:] = [
                    name for name in directories if name != "__pycache__"]
                for name in names:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(current, name)
                    relative = os.path.relpath(path, root)
                    files.append((relative, path))
        digest = hashlib.sha256()
        for relative, path in sorted(files):
            digest.update(relative.replace(os.sep, "/").encode("utf-8"))
            digest.update(b"\0")
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(128 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        return digest.hexdigest()

    def _attach_context_recorder(self):
        manager = getattr(self.host, "operation_context", None)
        if manager is None:
            return
        self.context_recorder = OperationContextRecorder(manager)
        self.context_recorder.attach()

    def _detach_context_recorder(self):
        recorder = self.context_recorder
        if recorder is not None:
            recorder.detach()

    def _start_context_scenario(self, name, fixtures):
        if self.context_recorder is not None:
            self.context_recorder.start_scenario(name, fixtures)

    def _finish_context_scenario(self):
        if self.context_recorder is not None:
            self.context_recorder.finish_scenario()

    def _schedule(self, delay):
        self.reactor.register_callback(
            self._advance, self.reactor.monotonic() + max(0.0, delay))

    def _advance(self, eventtime):
        if not self.running:
            return
        if self.abort_requested:
            self._complete("aborted", "abort requested")
            return
        if self.step_index >= len(self.steps):
            self._complete("passed", None)
            return
        step = self.steps[self.step_index]
        self.phase = step["phase"]
        try:
            if self.worker is not None:
                self.worker.marker(self._marker(self.phase))
            kind = step["kind"]
            if kind == "call":
                step["callback"]()
                self._step_passed(step, step.get("delay", 0.15))
            elif kind in ("tap", "tap_label", "prompt_tap"):
                if kind == "tap":
                    action = step.get("action")
                elif kind == "tap_label":
                    action = self.scenarios._action_for_label(
                        step["button_label"])
                else:
                    action = self.scenarios._prompt_action_for_label(
                        step["button_label"])
                self._tap(action)
                self.reactor.register_callback(
                    lambda now, expected=step.get("page"), item=step:
                    self._after_tap(now, item, expected),
                    eventtime + 0.22)
            elif kind == "wait":
                if step["predicate"]():
                    self._step_passed(step, 0.05)
                else:
                    deadline = self.step_runtime.setdefault(
                        "wait_deadline", eventtime + step["timeout"])
                    if eventtime >= deadline:
                        raise RuntimeError("Timed out: %s" % step["label"])
                    self._schedule(step["interval"])
            elif kind == "capture":
                self._capture(step)
            else:
                raise RuntimeError("Unknown test step: %s" % kind)
        except Exception as exc:
            logging.exception("[feather_ui_test] step failed: %s", step["label"])
            self.failures.append({"step": step["label"], "error": str(exc)})
            self._event("FAILED %s: %s" % (step["label"], exc))
            self._complete("failed", str(exc))

    def _after_tap(self, eventtime, step, expected):
        if not self.running or self.finalizing:
            return
        try:
            if self.abort_requested:
                self._complete("aborted", "abort requested")
                return
            if self.host.page == Page.MESSAGE and expected != Page.MESSAGE:
                raise RuntimeError(str(getattr(
                    self.host, "message", "Action opened a message")))
            if self.host.page == Page.ERROR:
                raise RuntimeError(str(getattr(
                    self.host, "error_message", "Action opened an error")))
            expected_seen = self.step_runtime.get(
                "expected_page_seen", expected is None)
            if not expected_seen and self.host.page == expected:
                self.step_runtime["expected_page_seen"] = True
                expected_seen = True
            operation_active = (
                int(getattr(self.host, "command_depth", 0)) > 0
                or getattr(self.host, "busy_message", None) is not None)
            if operation_active:
                deadline = self.step_runtime.setdefault(
                    "operation_deadline",
                    eventtime + TAP_OPERATION_TIMEOUT)
                if eventtime >= deadline:
                    raise RuntimeError(
                        "Timed out waiting for action to finish: %s" %
                        step["label"])
                self.reactor.register_callback(
                    lambda now, target=expected, item=step:
                    self._after_tap(now, item, target),
                    eventtime + 0.1)
                return
            if not expected_seen:
                raise RuntimeError(
                    "Action %s did not reach page %s; stopped at %s" %
                    (step["label"], expected.name, self.host.page.name))
            self.step_index += 1
            self.step_runtime = {}
            self._event("PASS %s" % step["label"])
            self._schedule(0.02)
        except Exception as exc:
            self.failures.append({"step": step["label"], "error": str(exc)})
            self._complete("failed", str(exc))

    def _step_passed(self, step, delay, advance=True):
        if advance:
            self.step_index += 1
            self.step_runtime = {}
        self._event("PASS %s" % step["label"])
        if advance:
            self._schedule(delay)

    def _capture(self, step):
        if self.step_runtime.get("capture_pending"):
            return
        renderer_status = self.host.renderer.get_status()
        target = self.step_runtime.setdefault(
            "render_target", renderer_status["submitted_batches"])
        settled = (renderer_status["rendered_batches"]
                   + renderer_status["coalesced_batches"]
                   + renderer_status["dropped_batches"])
        if renderer_status["dropped_batches"] > self.renderer_dropped:
            raise RuntimeError("Render batch dropped before capture")
        if settled < target:
            deadline = self.step_runtime.setdefault(
                "render_deadline", self.reactor.monotonic() + 6.0)
            if self.reactor.monotonic() >= deadline:
                raise RuntimeError("Renderer did not settle before capture")
            self._schedule(0.02)
            return
        token = self.step_runtime.get("capture_receipt")
        if token is None:
            token = "ui-test:%s:%d:%d" % (
                self.run_id or os.getpid(), self.step_index,
                self.capture_number + 1)
            command = (
                "--batch flush --receipt %s --receipt-phase presented" %
                token)
            accepted = self.host.renderer.send(
                (command,), kind="state", key="ui-test-capture-barrier")
            if not accepted:
                raise RuntimeError("Unable to queue capture receipt")
            self.step_runtime["capture_receipt"] = token
            self.step_runtime["receipt_deadline"] = (
                self.reactor.monotonic() + CAPTURE_RECEIPT_TIMEOUT)
            self._schedule(0.02)
            return
        receipt = self.capture_receipts.get(token)
        if receipt is None:
            if self.reactor.monotonic() >= self.step_runtime[
                    "receipt_deadline"]:
                raise RuntimeError("Typer did not present frame before capture")
            self._schedule(0.02)
            return
        del self.capture_receipts[token]
        if not bool(getattr(receipt, "success", False)):
            raise RuntimeError("Typer failed to present frame before capture")
        self.step_runtime["capture_pending"] = True
        self.renderer_dropped = renderer_status["dropped_batches"]
        self.capture_number += 1
        metadata = self._screen_metadata()
        if step.get("case_id"):
            metadata["case_id"] = step["case_id"]
        self.worker.capture(
            self.capture_number, step["label"], metadata,
            lambda result, item=step: self._capture_finished(item, result))

    def _capture_finished(self, step, result):
        if not self.running or self.finalizing:
            return
        if isinstance(result, Exception):
            self.failures.append({"step": step["label"], "error": str(result)})
            self._complete("failed", str(result))
            return
        self.step_index += 1
        self.step_runtime = {}
        self._event("CAPTURE %s %s" % (step["label"], result["file"]))
        self._schedule(0.02)

    def _screen_metadata(self):
        renderer = self.host.renderer
        renderer_status = renderer.get_status()
        buttons = {}
        for action, spec in getattr(renderer, "_buttons", {}).items():
            buttons[str(action)] = {
                "x": spec[0], "y": spec[1], "width": spec[2],
                "height": spec[3], "label": str(spec[4]),
                "state": str(spec[5]),
            }
            if (spec[0] < 0 or spec[1] < 0 or spec[2] <= 0 or spec[3] <= 0
                    or spec[0] + spec[2] > SCREEN_WIDTH
                    or spec[1] + spec[3] > SCREEN_HEIGHT):
                raise RuntimeError("Invalid hitbox: %s" % action)
        hitboxes = {}
        for action, spec in getattr(renderer, "_hitboxes", {}).items():
            hitboxes[str(action)] = {
                "x": spec[0], "y": spec[1], "width": spec[2],
                "height": spec[3], "continuous": bool(spec[4]),
            }
            if (spec[0] < 0 or spec[1] < 0 or spec[2] <= 0 or spec[3] <= 0
                    or spec[0] + spec[2] > SCREEN_WIDTH
                    or spec[1] + spec[3] > SCREEN_HEIGHT):
                raise RuntimeError("Invalid hitbox: %s" % action)
        return {
            "time": time.time(), "phase": self.phase,
            "page": self.host.page.name,
            "generation": getattr(renderer, "generation", None),
            "semantic_page_id": renderer_status.get("semantic_page_id"),
            "buttons": buttons,
            "hitboxes": hitboxes,
            "toggles": _jsonable(getattr(renderer, "_toggles", {})),
            "renderer": renderer_status,
            "temperatures": self._temperatures(),
            "position": self._position(),
        }

    def _temperatures(self):
        now = self.reactor.monotonic()
        nozzle = self.host.extruder.get_status(now)
        bed = self.host.heater_bed.get_status(now)
        values = {
            "time": time.time(), "nozzle": float(nozzle["temperature"]),
            "nozzle_target": float(nozzle["target"]),
            "bed": float(bed["temperature"]),
            "bed_target": float(bed["target"]),
        }
        if self.worker is not None:
            self.worker.log(
                "TEMPERATURE %s" % json.dumps(values, sort_keys=True))
            self.worker.telemetry(
                "temperatures",
                ("time", "nozzle", "nozzle_target", "bed", "bed_target"),
                values)
        return values

    def _position(self):
        status = self.host.toolhead.get_status(self.reactor.monotonic())
        values = [float(value) for value in status.get("position", ())[:3]]
        if self.worker is not None:
            self.worker.log("POSITION %s" % json.dumps(values))
            self.worker.telemetry(
                "positions", ("time", "x", "y", "z"),
                {"time": time.time(),
                 "x": values[0] if len(values) > 0 else "",
                 "y": values[1] if len(values) > 1 else "",
                 "z": values[2] if len(values) > 2 else ""})
        return values

    def _tap(self, action):
        action = str(action)
        renderer = self.host.renderer
        interactive = (
            action in getattr(renderer, "_buttons", {})
            or action in getattr(renderer, "_toggles", {})
            or action in getattr(renderer, "_hitboxes", {}))
        if not interactive:
            raise RuntimeError("Button is absent or disabled: %s" % action)
        if action in PERSISTENT_ACTIONS:
            raise RuntimeError("Persistent action is forbidden: %s" % action)
        self.host._start_touch_action(action)

    def _temperature_wait_active(self):
        wait = getattr(self.host, "temperature_wait", None)
        return bool(wait is not None and getattr(
            wait, "variables", {}).get("active", False))

    def _restore_context_runtime(self):
        if self.context_fixture is not None:
            self.context_fixture.restore(self.suite)

    def _restore_state(self):
        first_error = None
        try:
            self._restore_context_runtime()
        except Exception as exc:
            first_error = exc
        try:
            if self.snapshot is not None:
                self.snapshot.restore(
                    self.host, self.reactor,
                    self.suite not in NONPHYSICAL_SUITES)
        except Exception as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error

    def _complete(self, outcome, reason):
        if not self.running or self.finalizing:
            return
        self.finalizing = True
        try:
            self._restore_state()
        except Exception as exc:
            self.failures.append({"step": "cleanup", "error": str(exc)})
            outcome = "failed"
            reason = reason or ("cleanup failed: %s" % exc)
        recorder = self.context_recorder
        if recorder is not None:
            recorder.abort_active(reason or outcome)
            operation_context = recorder.report()
            recorder.detach()
        else:
            operation_context = {"passed": True, "scenarios": []}
        context_summary = {
            "passed": operation_context["passed"],
            "scenario_count": len(operation_context["scenarios"]),
            "scenarios": [{
                "scenario": item.get("scenario"),
                "passed": item.get("passed", False),
                "fixture": item.get("fixture"),
                "variant": item.get("variant"),
                "diagnostic": item.get("diagnostic"),
            } for item in operation_context["scenarios"]],
        }
        summary = {
            "run_id": self.run_id, "suite": self.suite,
            "material": self.material, "outcome": outcome,
            "reason": reason, "failures": self.failures,
            "calibration_stages": self.calibration_stages,
            "test_results": self.test_results,
            "operation_context": context_summary,
            "_operation_context_artifact": operation_context,
            "started_at": self.started_at, "finished_at": time.time(),
            "duration": time.time() - self.started_at,
        }
        self.phase = "finalizing"
        self.worker.finish(summary, self._finished)

    def _finished(self, result):
        self._detach_context_recorder()
        outcome = "failed"
        if isinstance(result, Exception):
            message = "artifact finalization failed: %s" % result
        else:
            outcome = result.get("outcome", "failed")
            message = result.get("reason") or outcome
        gcmd = self.gcmd
        directory = self.run_directory
        screenshots = (0 if isinstance(result, Exception)
                       else result.get("screenshots", 0))
        self.running = False
        self.finalizing = False
        self.phase = "idle"
        self.gcmd = None
        if self.worker is not None:
            self.worker.stop()
        self.worker = None
        if gcmd is not None:
            gcmd.respond_info(
                "Feather UI test %s: %s; screenshots=%d; artifacts=%s" %
                (outcome, message, screenshots, directory))
        if self.on_finished is not None:
            self.on_finished(self)

    def _event(self, message):
        logging.info("[feather_ui_test] %s", message)
        if self.worker is not None:
            self.worker.log(message)

    def deactivate(self):
        if self.running and not self.finalizing:
            self._complete("aborted", "feature deactivated")
            return
        self.abort_requested = True
        if self.context_recorder is not None:
            self.context_recorder.abort_active("feature deactivated")
            self.context_recorder.detach()
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        self.running = False
