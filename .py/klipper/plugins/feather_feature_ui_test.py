## Lazy on-printer regression runner for the Feather display.
##
## This module is deliberately absent from Feather's normal import graph.  It
## is loaded only by the hidden _FEATHER_UI_TEST command.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import base64
import binascii
import hashlib
import importlib
import json
import logging
import math
import os
import queue
import re
import shutil
import struct
import threading
import time
from datetime import datetime

from ui import Page, PrintState
from ff5m_ui.move import actions as move_actions
from ff5m_ui.z_offset import actions as z_actions
from feather_operation_context_fixtures import OperationContextRecorder


ARTIFACT_ROOT = "/data/feather-ui-tests"
ACTIVE_MARKER = os.path.join(ARTIFACT_ROOT, "active.json")
FRAMEBUFFER = "/dev/fb0"
FRAMEBUFFER_PAN = "/sys/class/graphics/fb0/pan"
FRAMEBUFFER_STRIDE = "/sys/class/graphics/fb0/stride"
PRINTER_LOG = "/data/logFiles/printer.log"
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 480
FRAME_BYTES_PER_PIXEL = 4
FRAME_BYTES = SCREEN_WIDTH * SCREEN_HEIGHT * 4
FRAME_PAN_RETRIES = 4
FRAME_SAMPLE_INTERVAL = 0.05
FRAME_SETTLE_INTERVAL = 0.25
FRAME_SETTLE_TIMEOUT = 3.0
CAPTURE_RECEIPT_TIMEOUT = 6.0
MAX_RUNS = 10
MAX_BYTES = 512 * 1024 * 1024
TAP_OPERATION_TIMEOUT = 1900.0
MOTION_STEP_TIMEOUT = 10.0
MOTION_STEP_INTERVAL = 0.1
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
COMPONENT_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
MAX_COMPONENT_CASE_BYTES = 32 * 1024
MAX_COMPONENT_CASES = 64


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return dict((str(key), _jsonable(item))
                    for key, item in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    name = getattr(value, "name", None)
    return str(name if name is not None else value)


def _bounded_component_value(value, depth=0):
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= 256
    if depth >= 3:
        return False
    if isinstance(value, list):
        return len(value) <= 32 and all(
            _bounded_component_value(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return len(value) <= 32 and all(
            isinstance(key, str) and len(key) <= 128
            and _bounded_component_value(item, depth + 1)
            for key, item in value.items())
    return False


def _atomic_json(path, value):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(_jsonable(value), stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _directory_size(path):
    total = 0
    for root, _directories, files in os.walk(path):
        for filename in files:
            try:
                total += os.path.getsize(os.path.join(root, filename))
            except OSError:
                pass
    return total


class ArtifactWorker:
    """Perform framebuffer and artifact IO away from Klipper's reactor."""

    def __init__(self, reactor, run_directory, framebuffer=FRAMEBUFFER,
                 printer_log=PRINTER_LOG, framebuffer_pan=FRAMEBUFFER_PAN,
                 framebuffer_stride=FRAMEBUFFER_STRIDE):
        self.reactor = reactor
        self.run_directory = run_directory
        self.framebuffer = framebuffer
        self.framebuffer_pan = framebuffer_pan
        self.framebuffer_stride = framebuffer_stride
        self.printer_log = printer_log
        self.tasks = queue.Queue()
        self.records = []
        self.log_start = self._file_size(printer_log)
        self.thread = threading.Thread(
            target=self._work, name="feather-ui-artifacts")
        self.thread.daemon = True
        self.thread.start()

    @staticmethod
    def _file_size(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _reactor_callback(self, callback, value):
        def deliver(_eventtime):
            callback(value)
        register = getattr(self.reactor, "register_async_callback", None)
        if register is not None:
            register(deliver)
        else:
            self.reactor.register_callback(deliver)

    def log(self, message):
        self.tasks.put(("log", str(message), None))

    def marker(self, value):
        self.tasks.put(("marker", value, None))

    def telemetry(self, name, fields, values):
        self.tasks.put(("telemetry", (name, fields, values), None))

    def capture(self, number, label, metadata, callback):
        self.tasks.put(("capture", (number, label, metadata), callback))

    def finish(self, summary, callback):
        self.tasks.put(("finish", summary, callback))

    def stop(self):
        self.tasks.put(("stop", None, None))

    def _work(self):
        while True:
            kind, payload, callback = self.tasks.get()
            try:
                if kind == "stop":
                    return
                if kind == "log":
                    self._append_log(payload)
                    continue
                if kind == "marker":
                    _atomic_json(ACTIVE_MARKER, payload)
                    continue
                if kind == "telemetry":
                    self._append_telemetry(*payload)
                    continue
                if kind == "capture":
                    value = self._capture(*payload)
                elif kind == "finish":
                    value = self._finish(payload)
                else:
                    raise RuntimeError("Unknown artifact task: %s" % kind)
            except Exception as exc:
                logging.exception("[feather_ui_test] artifact task failed")
                value = exc
            if callback is not None:
                self._reactor_callback(callback, value)

    def _append_log(self, message):
        with open(os.path.join(self.run_directory, "run.log"), "a",
                  encoding="utf-8") as stream:
            stream.write("%s %s\n" % (
                datetime.now().isoformat(timespec="milliseconds"), message))

    def _append_telemetry(self, name, fields, values):
        path = os.path.join(self.run_directory, "%s.csv" % name)
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as stream:
            if new_file:
                stream.write(",".join(fields) + "\n")
            stream.write(",".join(str(values.get(field, ""))
                                  for field in fields) + "\n")

    def _read_pan(self):
        try:
            with open(self.framebuffer_pan, "r", encoding="ascii") as stream:
                fields = stream.read(64).strip().split(",")
        except OSError as exc:
            if self.framebuffer == FRAMEBUFFER:
                raise RuntimeError("Framebuffer pan is unavailable") from exc
            return 0, 0
        try:
            if len(fields) != 2:
                raise ValueError()
            xoffset, yoffset = (int(field) for field in fields)
        except ValueError as exc:
            raise RuntimeError("Framebuffer pan is invalid") from exc
        if xoffset < 0 or yoffset < 0:
            raise RuntimeError("Framebuffer pan is invalid")
        return xoffset, yoffset

    def _read_stride(self):
        try:
            with open(self.framebuffer_stride, "r",
                      encoding="ascii") as stream:
                stride = int(stream.read(64).strip())
        except OSError as exc:
            if self.framebuffer == FRAMEBUFFER:
                raise RuntimeError(
                    "Framebuffer stride is unavailable") from exc
            stride = SCREEN_WIDTH * FRAME_BYTES_PER_PIXEL
        except ValueError as exc:
            raise RuntimeError("Framebuffer stride is invalid") from exc
        if stride < SCREEN_WIDTH * FRAME_BYTES_PER_PIXEL:
            raise RuntimeError("Framebuffer stride is invalid")
        return stride

    def _read_frame_at(self, pan, stride):
        xoffset, yoffset = pan
        row_bytes = SCREEN_WIDTH * FRAME_BYTES_PER_PIXEL
        if (xoffset + SCREEN_WIDTH) * FRAME_BYTES_PER_PIXEL > stride:
            raise RuntimeError("Framebuffer pan exceeds its stride")
        rows = []
        with open(self.framebuffer, "rb", buffering=0) as stream:
            if stride == row_bytes and xoffset == 0:
                stream.seek(yoffset * stride)
                data = stream.read(FRAME_BYTES)
                if len(data) != FRAME_BYTES:
                    raise RuntimeError(
                        "Framebuffer has %d bytes, expected %d" %
                        (len(data), FRAME_BYTES))
                return data
            for row in range(SCREEN_HEIGHT):
                stream.seek(
                    (yoffset + row) * stride
                    + xoffset * FRAME_BYTES_PER_PIXEL)
                data = stream.read(row_bytes)
                if len(data) != row_bytes:
                    raise RuntimeError(
                        "Framebuffer row has %d bytes, expected %d" %
                        (len(data), row_bytes))
                rows.append(data)
        return b"".join(rows)

    def _read_frame(self):
        stride = self._read_stride()
        for _attempt in range(FRAME_PAN_RETRIES):
            before = self._read_pan()
            data = self._read_frame_at(before, stride)
            if self._read_pan() == before:
                self.last_frame_geometry = {
                    "xoffset": before[0], "yoffset": before[1],
                    "stride": stride,
                }
                return data
        raise RuntimeError("Framebuffer page changed during capture")

    def _stable_frame(self):
        deadline = time.monotonic() + FRAME_SETTLE_TIMEOUT
        previous = None
        changed_at = time.monotonic()
        data = None
        while True:
            data = self._read_frame()
            digest = hashlib.sha256(data).hexdigest()
            now = time.monotonic()
            if digest != previous:
                previous = digest
                changed_at = now
            elif now - changed_at >= FRAME_SETTLE_INTERVAL:
                return data, digest
            if now >= deadline:
                return data, digest
            # Typer may still be consuming later protocol frames after the
            # render worker has completed its FIFO writes.  Require a quiet
            # framebuffer window instead of accepting the first equal pair;
            # this wait belongs only to the artifact thread, never Klipper's
            # reactor.
            time.sleep(FRAME_SAMPLE_INTERVAL)

    @staticmethod
    def _bmp(data):
        size = 54 + len(data)
        header = struct.pack("<2sIHHI", b"BM", size, 0, 0, 54)
        header += struct.pack(
            "<IiiHHIIIIII", 40, SCREEN_WIDTH, -SCREEN_HEIGHT,
            1, 32, 0, len(data), 2835, 2835, 0, 0)
        return header + data

    def _capture(self, number, label, metadata):
        data, digest = self._stable_frame()
        if not any(data):
            raise RuntimeError("Framebuffer is blank")
        safe_label = "".join(
            char.lower() if char.isalnum() else "_" for char in label)
        safe_label = "_".join(filter(None, safe_label.split("_")))[:48]
        filename = "%03d-%s.bmp" % (number, safe_label or "screen")
        with open(os.path.join(self.run_directory, filename), "wb") as stream:
            stream.write(self._bmp(data))
        record = dict(metadata)
        record.update({
            "number": number, "label": label, "file": filename,
            "sha256": digest, "frame_bytes": len(data), "passed": True,
            "framebuffer": getattr(self, "last_frame_geometry", None),
        })
        self.records.append(record)
        _atomic_json(os.path.join(self.run_directory, "manifest.json"),
                     self.records)
        return record

    def _copy_printer_log(self):
        if not os.path.isfile(self.printer_log):
            return None
        current_size = os.path.getsize(self.printer_log)
        offset = self.log_start if current_size >= self.log_start else 0
        target = os.path.join(self.run_directory, "printer.log")
        with open(self.printer_log, "rb") as source:
            source.seek(offset)
            with open(target, "wb") as destination:
                shutil.copyfileobj(source, destination, 64 * 1024)
        return os.path.basename(target)

    def _finish(self, summary):
        summary = dict(summary)
        operation_context = summary.pop("_operation_context_artifact", None)
        if operation_context is not None:
            _atomic_json(os.path.join(
                self.run_directory, "operation_context.json"),
                operation_context)
        summary["screenshots"] = len(self.records)
        summary["printer_log"] = self._copy_printer_log()
        _atomic_json(os.path.join(self.run_directory, "summary.json"), summary)
        try:
            os.unlink(ACTIVE_MARKER)
        except OSError:
            pass
        self._retain()
        return summary

    def _retain(self):
        root = os.path.dirname(self.run_directory)
        entries = []
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return
        for name in names:
            path = os.path.join(root, name)
            if path == self.run_directory or not os.path.isdir(path):
                continue
            if re.match(
                    r"^\d{8}-\d{6}-\d{6}-(full|ui|component|render|motion|heat|screws|mesh|z|context_print|context_material)$",
                    name) is None:
                continue
            summary_path = os.path.join(path, "summary.json")
            if not os.path.isfile(summary_path):
                continue
            try:
                with open(summary_path, "r", encoding="utf-8") as stream:
                    outcome = json.load(stream).get("outcome")
            except Exception:
                outcome = "failed"
            entries.append({
                "path": path, "name": name, "size": _directory_size(path),
                "failed": outcome != "passed",
            })
        # Preserve the newest failed run for post-mortem inspection.
        newest_failed = next((item["path"] for item in reversed(entries)
                              if item["failed"]), None)
        total = sum(item["size"] for item in entries)
        while entries and (len(entries) + 1 > MAX_RUNS
                           or total + _directory_size(
                               self.run_directory) > MAX_BYTES):
            victim = next((item for item in entries
                           if item["path"] != newest_failed), None)
            if victim is None:
                break
            entries.remove(victim)
            total -= victim["size"]
            shutil.rmtree(victim["path"])


class UITestFeature:
    name = "ui_test"

    def __init__(self, host):
        self.host = host
        self.session_id = "%d-%.6f" % (os.getpid(), time.time())
        self.running = False
        self.dispatching_test_action = False
        self.abort_requested = False
        self.suite = None
        self.material = None
        self.phase = "idle"
        self.steps = []
        self.step_index = 0
        self.step_deadline = None
        self.gcmd = None
        self.run_id = None
        self.run_directory = None
        self.worker = None
        self.capture_number = 0
        self.failures = []
        self.started_at = None
        self.original = {}
        self.motion_origin = None
        self.motion_expected = None
        self.heat_initial = None
        self.heat_stable_since = None
        self._mesh_snapshot = None
        self.finalizing = False
        self._last_stage_status = None
        self.calibration_stages = []
        self.test_results = {}
        self.renderer_dropped = 0
        self.ui_filament_target = None
        self.capture_receipts = {}
        self.context_recorder = None
        self.context_files = []
        self.context_runtime = {}
        self.context_checkpoint = None

    @property
    def reactor(self):
        return self.host.reactor

    @property
    def input_blocked(self):
        # Immediate emergency actions are checked before this product lock.
        return self.running

    @property
    def theme_update_blocked(self):
        return self.running

    def blocks_action(self, action):
        return self.running and str(action) in PERSISTENT_ACTIONS

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
        self.component_cases = self._decode_component_cases(
            encoded_cases) if suite == "COMPONENT" else ()
        self.material = self._resolve_material(material, suite)
        self.gcmd = gcmd
        self.started_at = time.time()
        self.abort_requested = False
        self.failures = []
        self.step_index = 0
        self.step_deadline = None
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
        os.makedirs(ARTIFACT_ROOT, exist_ok=True)
        if shutil.disk_usage(ARTIFACT_ROOT).free < 64 * 1024 * 1024:
            raise gcmd.error("Less than 64 MiB is free below /data")
        self.run_directory = os.path.join(ARTIFACT_ROOT, self.run_id)
        os.makedirs(self.run_directory)
        _atomic_json(ACTIVE_MARKER, self._marker("starting"))
        _atomic_json(os.path.join(self.run_directory, "environment.json"),
                     self._environment())
        self.worker = ArtifactWorker(self.reactor, self.run_directory)
        self.steps = self._build_steps(suite)
        try:
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
            raise

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
        now = self.reactor.monotonic()
        mesh = getattr(self.host, "bed_mesh", None)
        mesh_status = mesh.get_status(now) if mesh is not None else {}
        extruder = self.host.extruder.get_status(now)
        heater_bed = self.host.heater_bed.get_status(now)
        fan = getattr(self.host, "fan", None)
        fan_status = fan.get_status(now) if fan is not None else {}
        self.original = {
            "page": self.host.page,
            "previous_page": self.host.previous_page,
            "filament_material": self.host.filament_material,
            "runtime_z": float(self.host.gcode_move.get_status(now)[
                "homing_origin"][2]),
            "mesh_object": getattr(mesh, "z_mesh", None),
            "mesh_profile": str(mesh_status.get("profile_name", "") or ""),
            "extruder_target": float(extruder.get("target", 0.0)),
            "bed_target": float(heater_bed.get("target", 0.0)),
            "fan_speed": float(fan_status.get("speed", 0.0) or 0.0),
        }

    def _marker(self, phase):
        return {
            "run_id": self.run_id, "pid": os.getpid(), "suite": self.suite,
            "session": self.session_id,
            "hardware": self.suite not in NONPHYSICAL_SUITES,
            "phase": phase, "runtime_z": self.original.get("runtime_z", 0.0),
            "mesh_profile": self.original.get("mesh_profile", ""),
            "directory": self.run_directory, "updated_at": time.time(),
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
        root = os.path.dirname(os.path.abspath(__file__))
        files = []
        for relative in UI_FINGERPRINT_FILES:
            path = os.path.join(root, relative)
            if os.path.isfile(path):
                files.append((relative, path))
        for package in ("ui", "ff5m_ui"):
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

    def _build_steps(self, suite):
        steps = []
        self._add_capture(steps, "baseline")
        phases = (["ui", "render", "motion", "heat", "screws", "mesh", "z"]
                  if suite == "FULL" else [suite.lower()])
        for phase in phases:
            if phase in ("context_print", "context_material"):
                getattr(self, "_steps_%s" % phase)(steps)
                continue
            fixtures = {
                "screws": ("screws",),
                "mesh": ("mesh_clean", "mesh_skip_clean"),
                "z": ("z_offset_skip_clean",),
            }.get(phase, ("none",))
            self._add_call(
                steps, "%s-context-start" % phase,
                lambda name=phase, choices=fixtures:
                self._start_context_scenario(name, choices), delay=0.0)
            getattr(self, "_steps_%s" % phase)(steps)
            self._add_call(
                steps, "%s-context-verify" % phase,
                self._finish_context_scenario, delay=0.0)
        return steps

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

    @staticmethod
    def _add_call(steps, label, callback, delay=0.15):
        steps.append({"kind": "call", "label": label,
                      "callback": callback, "delay": delay})

    @staticmethod
    def _add_tap(steps, action, page=None, label=None):
        steps.append({"kind": "tap", "action": action, "page": page,
                      "label": label or str(action)})

    @classmethod
    def _add_semantic_tap(cls, steps, action, page=None):
        """Tap the exact deterministic wire identity of a typed command."""
        cls._add_tap(
            steps, action.wire_id, page,
            label=str(action.key.value))

    @staticmethod
    def _add_tap_label(steps, button_label, page=None):
        steps.append({"kind": "tap_label", "button_label": button_label,
                      "page": page, "label": button_label})

    @staticmethod
    def _add_wait(steps, label, predicate, timeout, interval=0.5):
        steps.append({"kind": "wait", "label": label,
                      "predicate": predicate, "timeout": timeout,
                      "interval": interval})

    @staticmethod
    def _add_capture(steps, label):
        steps.append({"kind": "capture", "label": label})

    @staticmethod
    def _add_case_capture(steps, label, case_id):
        steps.append({
            "kind": "capture", "label": label, "case_id": case_id,
        })

    def _steps_ui(self, steps):
        self._add_call(steps, "ui-pause-timer", self._pause_ui_timer)
        self._add_call(steps, "ui-home-filled", self._render_filled_home)
        self._add_capture(steps, "ui-home-filled")
        self._add_call(steps, "ui-home", lambda: self._show(Page.IDLE_HOME))
        self._add_capture(steps, "ui-home")
        self._add_tap(steps, "nav.filament", Page.FILAMENT_MATERIAL)
        self._add_tap(steps, "nav.back", Page.IDLE_HOME)
        self._add_tap(steps, "nav.move", Page.CONTROL_MOVE)
        self._add_tap(steps, "nav.back", Page.IDLE_HOME)
        self._add_tap(steps, "nav.menu", Page.MAIN_MENU)
        self._add_capture(steps, "ui-main-menu")
        self._add_tap(steps, "nav.files", Page.FILE_BROWSER)
        self._add_capture(steps, "ui-files")
        self._add_call(steps, "ui-file-confirm", self._open_safe_file_confirm)
        self._add_capture(steps, "ui-file-confirm")
        self._add_call(steps, "ui-file-return", self._return_from_file_confirm)
        # The internal file browser belongs to the home screen, so its Back
        # action returns there rather than to the menu used to open it.
        self._add_tap(steps, "nav.back", Page.IDLE_HOME)
        self._add_tap(steps, "nav.menu", Page.MAIN_MENU)
        self._add_tap(steps, "nav.control", Page.CONTROL_HOME)
        self._add_capture(steps, "ui-control")
        self._add_tap(steps, "nav.move", Page.CONTROL_MOVE)
        self._add_capture(steps, "ui-move")
        self._add_tap(steps, "nav.back", Page.CONTROL_HOME)
        self._add_tap(steps, "nav.heat", Page.CONTROL_HEAT)
        self._add_capture(steps, "ui-heat")
        self._add_tap(steps, "nav.back", Page.CONTROL_HOME)
        self._add_tap(steps, "nav.calibration", Page.CALIBRATION_HOME)
        self._add_capture(steps, "ui-calibration")
        self._add_call(steps, "ui-calibration-pages",
                       self._render_calibration_variants)
        self._add_capture(steps, "ui-calibration-variants")
        self._add_tap(steps, "nav.back", Page.CONTROL_HOME)
        self._add_tap(steps, "nav.settings", Page.SETTINGS)
        self._add_capture(steps, "ui-settings")
        self._add_tap(steps, "settings.mod", Page.MOD_SETTINGS)
        self._add_capture(steps, "ui-mod-parameters")
        self._add_tap(steps, "nav.back", Page.SETTINGS)
        self._add_tap(steps, "nav.back", Page.CONTROL_HOME)
        self._add_tap(steps, "nav.back", Page.MAIN_MENU)
        self._add_tap(steps, "nav.filament", Page.FILAMENT_MATERIAL)
        self._add_capture(steps, "ui-filament-materials")
        self._add_call(steps, "ui-filament-action",
                       self._render_safe_filament_action)
        self._add_capture(steps, "ui-filament-action")
        self._add_call(steps, "ui-filament-cooling",
                       self._render_safe_filament_cooling)
        self._add_capture(steps, "ui-filament-cooling")
        self._add_call(steps, "ui-filament-target",
                       self._remember_ui_filament_target)
        self._add_tap(steps, "nav.back", Page.FILAMENT_MATERIAL)
        self._add_call(steps, "ui-filament-target-preserved",
                       self._assert_ui_filament_target_preserved)
        self._add_capture(steps, "ui-filament-back-materials")
        self._add_tap(steps, "nav.back", Page.MAIN_MENU)
        self._add_tap(steps, "nav.network", Page.NETWORK_HOME)
        self._add_capture(steps, "ui-network")
        self._add_tap(steps, "nav.back", Page.MAIN_MENU)
        self._add_tap(steps, "nav.back", Page.IDLE_HOME)
        self._add_call(steps, "ui-resume-timer", self._resume_ui_timer)

    @staticmethod
    def _component_pages():
        """Discover module-level declarative pages only in the cold test path."""
        import ff5m_ui as package
        from ui.layout import DeclarativePage
        package_root = os.path.dirname(package.__file__)
        modules = set()
        for root, directories, files in os.walk(package_root):
            directories[:] = [
                name for name in directories if name != "__pycache__"]
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                relative = os.path.relpath(
                    os.path.join(root, filename), package_root)
                parts = relative[:-3].split(os.sep)
                if parts[-1] == "__init__":
                    parts.pop()
                modules.add(package.__name__ + (
                    "." + ".".join(parts) if parts else ""))
        pages = {}
        for module_name in sorted(modules):
            module = importlib.import_module(module_name)
            for value in vars(module).values():
                if not isinstance(value, DeclarativePage):
                    continue
                pages.setdefault(value.page_id, value)
        return tuple(pages[key] for key in sorted(pages))

    def _steps_component(self, steps):
        self._add_call(
            steps, "component-pause-timer", self._pause_ui_timer)
        for page in self._component_pages():
            slug = re.sub(
                r"[^a-z0-9]+", "-",
                page.page_id.rsplit(".", 1)[-1].lower()).strip("-")
            case_id = "default-" + slug
            self._add_call(
                steps, "component-render-" + slug,
                lambda item=page: self._render_component_default(item))
            self._add_case_capture(
                steps, "component-" + case_id, case_id)
        for case in getattr(self, "component_cases", ()):
            self._add_call(
                steps, "component-render-" + case["id"],
                lambda item=case: self._render_component_case(item))
            self._add_case_capture(
                steps, "component-" + case["id"], case["id"])
        self._add_call(
            steps, "component-resume-timer", self._resume_ui_timer)

    def _render_component_default(self, page):
        title = self._component_title(page)
        commands = self.host.renderer.begin_page(title, back=False)
        commands += page.draw(self.host.renderer, {})
        self.host.renderer.send(commands)
        self._render_component_footer()

    def _render_component_case(self, case):
        page = case["page"]
        title = self._component_title(page)
        commands = self.host.renderer.begin_page(title, back=False)
        commands += page.draw(self.host.renderer, case["state"])
        self.host.renderer.send(commands)
        self._render_component_footer()

    @staticmethod
    def _component_title(page):
        title = getattr(page, "title", None)
        if title:
            return str(title)
        title = str(getattr(page.page_key, "value", page.page_key))
        return title.replace("_", " ").replace(".", " / ")

    def _render_component_footer(self):
        self.host.renderer.footer(
            25.0, 0.0, 25.0, 0.0, "PREVIEW", "IDLE")

    def _decode_component_cases(self, encoded):
        if not encoded:
            return ()
        if len(encoded) > MAX_COMPONENT_CASE_BYTES * 2:
            raise ValueError("COMPONENT cases payload is too large")
        try:
            padding = "=" * (-len(encoded) % 4)
            raw = base64.urlsafe_b64decode(
                (encoded + padding).encode("ascii"))
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ValueError("COMPONENT cases payload is invalid") from exc
        if len(raw) > MAX_COMPONENT_CASE_BYTES:
            raise ValueError("COMPONENT cases payload is too large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("COMPONENT cases payload is invalid") from exc
        if not isinstance(value, list) or len(value) > MAX_COMPONENT_CASES:
            raise ValueError("COMPONENT cases must be a bounded array")
        pages = dict((page.page_id, page)
                     for page in self._component_pages())
        result = []
        seen = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {
                    "id", "page", "state"}:
                raise ValueError(
                    "COMPONENT case accepts only id, page, and state")
            case_id = str(item["id"])
            page_id = str(item["page"])
            state = item["state"]
            if (not COMPONENT_CASE_ID.match(case_id) or case_id in seen
                    or page_id not in pages or not isinstance(state, dict)
                    or len(state) > 32):
                raise ValueError("COMPONENT case is invalid")
            page = pages[page_id]
            schema = dict((str(key), key) for key in page.state_schema)
            typed = {}
            for name, raw_value in state.items():
                key = schema.get(str(name))
                if key is None or not _bounded_component_value(raw_value):
                    raise ValueError(
                        "COMPONENT state is not bounded declared state: %s" %
                        name)
                typed[key] = raw_value
            # StateStore validation checks types, choices and numeric bounds.
            page._fresh_state(typed)
            seen.add(case_id)
            result.append({"id": case_id, "page": page, "state": typed})
        return tuple(result)

    def _render_filled_home(self):
        """Render a worst-case dashboard without mutating printer hardware."""
        host = self.host

        class Status:
            def __init__(self, values):
                self.values = values

            def get_status(self, _eventtime):
                return dict(self.values)

        class VirtualSD:
            def is_active(self):
                return True

            def file_path(self):
                return "/data/THIS-IS-A-VERY-LONG-PRINT-FILENAME-FOR-UI.gcode"

        names = (
            "extruder", "heater_bed", "toolhead", "network_status",
            "last_job_name", "print_state", "print_stats", "virtual_sdcard",
            "_current_material", "_print_progress", "_print_time_values",
            "print_status_text", "_last_dashboard",
        )
        original = dict((name, getattr(host, name)) for name in names)
        try:
            host.extruder = Status({"temperature": 299.0, "target": 300.0})
            host.heater_bed = Status({"temperature": 129.0, "target": 130.0})
            host.toolhead = Status({"homed_axes": "xyz"})
            host.network_status = {
                "mode": "WIFI", "ssid": "VERY-LONG-WIRELESS-NETWORK-NAME",
                "signal": "100%", "ip": "255.255.255.255",
            }
            host.last_job_name = (
                "THIS-IS-A-VERY-LONG-PREVIOUS-PRINT-FILENAME.gcode")
            host.print_state = PrintState.PREPARING
            host.print_stats = Status({
                "state": "printing", "print_duration": 359999.0,
                "info": {"current_layer": 9999, "total_layer": 9999},
            })
            host.virtual_sdcard = VirtualSD()
            host._current_material = lambda: "CARBON-FIBER-POLYCARBONATE"
            host._print_progress = lambda _eventtime, _stats: 1.0
            host._print_time_values = (
                lambda _eventtime, _stats, _progress: (359999.0, 359999.0))
            host.print_status_text = "CALIBRATING AND PREPARING PRINT SURFACE"
            host._last_dashboard = None
            host._render_home()
        finally:
            for name, value in original.items():
                setattr(host, name, value)

    def _steps_motion(self, steps):
        self._add_call(steps, "motion-open", lambda: self._show(Page.CONTROL_MOVE))
        self._add_capture(steps, "motion-before-home")
        self._add_call(steps, "motion-caution", self._dismiss_move_caution)
        self._add_semantic_tap(
            steps, move_actions.HOME_ALL, Page.CONTROL_MOVE)
        self._add_call(steps, "motion-origin", self._save_motion_origin)
        self._add_capture(steps, "motion-homed")
        for axis in "xyz":
            self._add_call(steps, "motion-%s-forward-dispatch" % axis,
                           lambda axis=axis: self._motion_step(axis, 1))
            self._add_wait(
                steps, "motion-%s-forward-complete" % axis,
                lambda axis=axis: self._motion_reached(axis),
                MOTION_STEP_TIMEOUT, MOTION_STEP_INTERVAL)
            self._add_capture(steps, "motion-%s-forward" % axis)
            self._add_call(steps, "motion-%s-return-dispatch" % axis,
                           lambda axis=axis: self._motion_step(axis, -1))
            self._add_wait(
                steps, "motion-%s-return-complete" % axis,
                lambda axis=axis: self._motion_reached(axis),
                MOTION_STEP_TIMEOUT, MOTION_STEP_INTERVAL)
            self._add_capture(steps, "motion-%s-return" % axis)
        self._add_call(steps, "motion-disable", lambda: self.host._run_script("M84"))

    def _steps_render(self, steps):
        before_status = self.host.renderer.get_status()
        before = before_status["typer_restarts"]
        before_error = before_status.get("worker_last_error", "")
        self._add_call(steps, "render-pause-timer", self._pause_ui_timer)
        self._add_call(
            steps, "render-restart-signal",
            lambda: self._request_renderer_restart(before))
        self._add_wait(
            steps, "render-recovered",
            lambda: self._renderer_recovered(before, before_error),
            15.0, 0.1)
        self._add_capture(steps, "render-recovered")
        self._add_call(steps, "render-resume-timer", self._resume_ui_timer)

    def _request_renderer_restart(self, before):
        if not self.host.renderer.restart():
            raise RuntimeError("Renderer restart signal was not accepted")

    def _renderer_recovered(self, before, before_error=""):
        status = self.host.renderer.get_status()
        return (status["worker_state"] == "running"
                and status["typer_restarts"] > before
                and status.get("worker_last_error", "") == before_error)

    def _steps_heat(self, steps):
        self._add_call(steps, "heat-open", lambda: self._show(Page.CONTROL_HEAT))
        self._add_call(steps, "heat-initial", self._save_heat_initial)
        self._add_capture(steps, "heat-cold")
        self._add_tap_label(steps, self.material, Page.CONTROL_HEAT)
        self._add_wait(steps, "heat-targets", self._heat_targets_set, 10.0)
        self._add_capture(steps, "heat-target-accepted")
        self._add_wait(steps, "heat-rising", self._heat_is_rising, 120.0, 1.0)
        self._add_capture(steps, "heat-rising")
        self._add_wait(steps, "heat-stable", self._heat_is_stable, 900.0, 2.0)
        self._add_capture(steps, "heat-target-reached")
        self._add_tap_label(steps, "COOLDOWN", Page.CONTROL_HEAT)
        self._add_wait(steps, "heat-off", self._heaters_off, 10.0)
        self._add_capture(steps, "heat-cooldown-start")

    def _steps_screws(self, steps):
        self._add_call(steps, "screws-open", self._open_calibration_home)
        self._add_tap(steps, "cal.screws", Page.CALIBRATION_CONFIRM)
        self._add_tap(steps, "cal.clean.skip", Page.CALIBRATION_CONFIRM)
        self._add_capture(steps, "screws-confirm")
        self._add_tap(steps, "cal.confirm", Page.CALIBRATION_PROGRESS)
        self._add_wait(steps, "screws-result", self._calibration_result, 1200.0, 1.0)
        self._add_capture(steps, "screws-result")
        self._add_tap(steps, "cal.done", Page.CALIBRATION_HOME)
        self._add_call(steps, "screws-cleanup", self._hardware_cleanup)

    def _steps_mesh(self, steps):
        self._add_call(steps, "mesh-snapshot", self._save_mesh_snapshot)
        self._add_call(steps, "mesh-open", self._open_calibration_home)
        self._add_tap(steps, "cal.mesh", Page.CALIBRATION_CONFIRM)
        self._add_capture(steps, "mesh-confirm")
        self._add_tap(steps, "cal.confirm", Page.CALIBRATION_PROGRESS)
        self._add_wait(steps, "mesh-result", self._calibration_result, 1800.0, 1.0)
        self._add_call(steps, "mesh-validate", self._validate_mesh)
        self._add_capture(steps, "mesh-result")
        self._add_tap(steps, "cal.mesh.discard", Page.CALIBRATION_HOME)
        self._add_call(steps, "mesh-restore", self._restore_mesh_snapshot)
        self._add_call(steps, "mesh-cleanup", self._hardware_cleanup)

    def _steps_z(self, steps):
        self._add_call(steps, "z-open", self._open_calibration_home)
        self._add_tap(steps, "cal.z", Page.CALIBRATION_CONFIRM)
        self._add_tap(steps, "cal.clean.skip", Page.CALIBRATION_CONFIRM)
        self._add_capture(steps, "z-confirm")
        self._add_tap(steps, "cal.confirm", Page.SAFE_Z_BRIEFING)
        self._add_capture(steps, "z-safe-briefing")
        self._add_semantic_tap(steps, z_actions.SAFE_SKIP)
        self._add_wait(steps, "z-preparation", self._z_summary_ready,
                       1200.0, 1.0)
        self._add_capture(steps, "z-summary-empty")
        self._add_semantic_tap(
            steps, z_actions.ZONE_ACTIONS["center"],
            Page.Z_OFFSET_PAPER_BRIEFING)
        self._add_capture(steps, "z-paper-briefing")
        self._add_semantic_tap(steps, z_actions.ENTER_ZONE)
        self._add_wait(steps, "z-positioned", self._z_paper_ready,
                       120.0, 0.5)
        self._add_capture(steps, "z-paper-before-probe")
        self._add_semantic_tap(
            steps, z_actions.PROBE, Page.Z_OFFSET_PAPER)
        self._add_call(steps, "z-dismiss-pressure", self._dismiss_pressure)
        self._add_call(steps, "z-probe-position", self._save_z_probe_position)
        self._add_capture(steps, "z-paper-probed")
        self._add_tap_label(steps, "0.100 MM", Page.Z_OFFSET_PAPER)
        # The real UI offers at most 0.100 mm. Ten FARTHER presses create the
        # requested 1 mm safety margin, then ten CLOSER presses return to the
        # post-probe point without ever crossing it.
        for _index in range(10):
            self._add_semantic_tap(
                steps, z_actions.FARTHER, Page.Z_OFFSET_PAPER)
        self._add_call(steps, "z-farther-verify", self._verify_z_farther)
        self._add_capture(steps, "z-paper-farther-1mm")
        for _index in range(10):
            self._add_semantic_tap(
                steps, z_actions.CLOSER, Page.Z_OFFSET_PAPER)
        self._add_call(steps, "z-return-verify", self._verify_z_return)
        self._add_capture(steps, "z-paper-returned")
        self._add_semantic_tap(steps, z_actions.ACCEPT)
        self._add_wait(steps, "z-summary-result", self._z_summary_ready,
                       120.0, 0.5)
        self._add_capture(steps, "z-summary-result")
        self._add_tap(steps, "nav.back", Page.Z_OFFSET_SUMMARY)
        self._add_capture(steps, "z-discard-dialog")
        self._add_semantic_tap(steps, z_actions.DISCARD_CONFIRM)
        self._add_wait(steps, "z-discarded", self._z_discarded,
                       120.0, 0.5)
        self._add_call(steps, "z-cleanup-verify", self._verify_z_cleanup)
        self._add_call(steps, "z-hardware-cleanup", self._hardware_cleanup)

    def _steps_context_material(self, steps):
        self._add_call(
            steps, "context_material-prepare",
            self._prepare_context_material)
        self._add_call(
            steps, "filament-context-start",
            lambda: self._start_context_scenario(
                "filament", ("filament",)), delay=0.0)
        self._add_call(
            steps, "filament-open",
            lambda: self.host._run_script("LOAD_MATERIAL"))
        self._add_prompt_tap(steps, self.material, Page.ACTION_PROMPT)
        for label in ("Load", "Purge", "Unload"):
            self._add_prompt_tap(steps, label, Page.ACTION_PROMPT)
        self._add_prompt_tap(steps, "Done")
        self._add_call(
            steps, "filament-context-verify",
            self._finish_context_scenario, delay=0.0)
        self._add_call(
            steps, "cold_pull-context-start",
            lambda: self._start_context_scenario(
                "cold_pull", ("cold_pull",)), delay=0.0)
        self._add_call(
            steps, "cold_pull-open",
            lambda: self.host._run_script("COLDPULL"))
        self._add_prompt_tap(
            steps, self._context_cold_pull_material(), None)
        self._add_call(
            steps, "cold_pull-context-verify",
            self._finish_context_scenario, delay=0.0)
        self._add_call(
            steps, "context_material-cleanup", self._hardware_cleanup)

    def _steps_context_print(self, steps):
        self._add_call(
            steps, "context_print-prepare", self._prepare_context_print)

        self._add_call(
            steps, "print_mesh-context-start",
            lambda: self._start_context_scenario(
                "print_mesh_resume", ("print_mesh_resume",)), delay=0.0)
        self._add_call(
            steps, "print_mesh-file-open",
            lambda: self._open_context_file(self.context_files[1]))
        self._add_tap(steps, "file.item0", Page.FILE_CONFIRM)
        self._add_tap(steps, "file.start")
        self._add_wait(
            steps, "print_mesh-started", self._context_print_controls_ready,
            1900.0, 0.5)
        self._add_tap(steps, "print.pause")
        self._add_call(
            steps, "print_mesh-pause-motion-complete",
            lambda: self.host._run_script("M400"))
        self._add_wait(
            steps, "print_mesh-paused", self._context_paused,
            10.0, 0.1)
        self._add_tap(steps, "print.resume")
        self._add_wait(
            steps, "print_mesh-resumed", self._context_print_controls_ready,
            10.0, 0.1)
        self._add_tap(
            steps, "print.pause", label="print_mesh-pause-for-recovery")
        self._add_call(
            steps, "print_mesh-recovery-pause-motion-complete",
            lambda: self.host._run_script("M400"))
        self._add_wait(
            steps, "print_mesh-recovery-paused", self._context_paused,
            10.0, 0.1)
        self._add_wait(
            steps, "print_mesh-idle-timeout", self._context_idle_timeout,
            30.0, 0.25)
        self._add_wait(
            steps, "print_mesh-checkpoint", self._context_checkpoint_ready,
            15.0, 0.25)
        self._add_call(
            steps, "print_mesh-cancel", self._cancel_context_print)
        self._add_wait(
            steps, "print_mesh-cancelled", self._context_cancelled,
            10.0, 0.1)
        self._add_tap(steps, "message.ok", Page.IDLE_HOME,
                      "print_mesh-cancelled-dismiss")
        self._add_call(
            steps, "print_mesh-activate-recovery",
            self._activate_context_recovery)
        self._add_call(
            steps, "print_mesh-context-verify",
            self._finish_context_scenario, delay=0.0)

        self._add_call(
            steps, "recovery-context-start",
            lambda: self._start_context_scenario(
                "recovery", ("recovery",)), delay=0.0)
        self._add_tap(
            steps, "recovery.restore", Page.RECOVERY_CONFIRM)
        self._add_tap(
            steps, "recovery.confirm", Page.CALIBRATION_PROGRESS)
        self._add_wait(
            steps, "recovery-printing", self._context_printing,
            1900.0, 0.5)
        self._add_wait(
            steps, "recovery-complete", self._context_print_complete,
            1900.0, 0.5)
        self._add_tap(steps, "message.ok", Page.IDLE_HOME,
                      "recovery-finished-dismiss")
        self._add_call(
            steps, "recovery-context-verify",
            self._finish_context_scenario, delay=0.0)

        self._add_call(
            steps, "print_kamp-context-start",
            lambda: self._start_context_scenario(
                "print_kamp", ("print_kamp",)), delay=0.0)
        self._add_call(
            steps, "print_kamp-file-open",
            lambda: self._open_context_file(self.context_files[0]))
        self._add_tap(steps, "file.item0", Page.FILE_CONFIRM)
        self._add_tap(steps, "file.start")
        self._add_wait(
            steps, "print_kamp-started", self._context_printing,
            1900.0, 0.5)
        self._add_wait(
            steps, "print_kamp-complete", self._context_print_complete,
            1900.0, 0.5)
        self._add_tap(steps, "message.ok", Page.IDLE_HOME,
                      "print_kamp-finished-dismiss")
        self._add_call(
            steps, "print_kamp-context-verify",
            self._finish_context_scenario, delay=0.0)
        self._add_call(
            steps, "context_print-cleanup", self._hardware_cleanup)

    @staticmethod
    def _add_prompt_tap(steps, button_label, page=None):
        steps.append({
            "kind": "prompt_tap", "button_label": button_label,
            "page": page, "label": "prompt-%s" % button_label,
        })

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
        self.phase = step["label"].split("-", 1)[0]
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
                    action = self._action_for_label(step["button_label"])
                else:
                    action = self._prompt_action_for_label(
                        step["button_label"])
                self._tap(action)
                self.reactor.register_callback(
                    lambda now, expected=step.get("page"), item=step:
                    self._after_tap(now, item, expected),
                    eventtime + 0.22)
            elif kind == "wait":
                if step["predicate"]():
                    self.step_deadline = None
                    self._step_passed(step, 0.05)
                else:
                    if self.step_deadline is None:
                        self.step_deadline = eventtime + step["timeout"]
                    if eventtime >= self.step_deadline:
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
            expected_seen = step.get("expected_page_seen", expected is None)
            if not expected_seen and self.host.page == expected:
                step["expected_page_seen"] = True
                expected_seen = True
            operation_active = (
                int(getattr(self.host, "command_depth", 0)) > 0
                or getattr(self.host, "busy_message", None) is not None)
            if operation_active:
                deadline = step.setdefault(
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
            self._event("PASS %s" % step["label"])
            self._schedule(0.02)
        except Exception as exc:
            self.failures.append({"step": step["label"], "error": str(exc)})
            self._complete("failed", str(exc))

    def _step_passed(self, step, delay, advance=True):
        if advance:
            self.step_index += 1
        self._event("PASS %s" % step["label"])
        if advance:
            self._schedule(delay)

    def _capture(self, step):
        if step.get("capture_pending"):
            return
        renderer_status = self.host.renderer.get_status()
        target = step.setdefault(
            "render_target", renderer_status["submitted_batches"])
        settled = (renderer_status["rendered_batches"]
                   + renderer_status["coalesced_batches"]
                   + renderer_status["dropped_batches"])
        if renderer_status["dropped_batches"] > self.renderer_dropped:
            raise RuntimeError("Render batch dropped before capture")
        if settled < target:
            deadline = step.setdefault(
                "render_deadline", self.reactor.monotonic() + 6.0)
            if self.reactor.monotonic() >= deadline:
                raise RuntimeError("Renderer did not settle before capture")
            self._schedule(0.02)
            return
        token = step.get("capture_receipt")
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
            step["capture_receipt"] = token
            step["receipt_deadline"] = (
                self.reactor.monotonic() + CAPTURE_RECEIPT_TIMEOUT)
            self._schedule(0.02)
            return
        receipt = self.capture_receipts.get(token)
        if receipt is None:
            if self.reactor.monotonic() >= step["receipt_deadline"]:
                raise RuntimeError("Typer did not present frame before capture")
            self._schedule(0.02)
            return
        del self.capture_receipts[token]
        if not bool(getattr(receipt, "success", False)):
            raise RuntimeError("Typer failed to present frame before capture")
        step["capture_pending"] = True
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
        self.dispatching_test_action = True
        try:
            self.host._handle_touch_action(action)
        finally:
            self.dispatching_test_action = False

    def _action_for_label(self, label):
        matches = [action for action, spec in self.host.renderer._buttons.items()
                   if str(spec[4]).upper() == str(label).upper()]
        if len(matches) != 1:
            raise RuntimeError(
                "Expected one button labelled %s, found %d" %
                (label, len(matches)))
        return matches[0]

    def _prompt_action_for_label(self, label):
        prompt = getattr(self.host, "action_prompt", None) or {}
        matches = [
            action for action, button in prompt.get("buttons", {}).items()
            if str(button.get("label", "")).casefold()
            == str(label).casefold()
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "Expected one action-prompt button labelled %s, found %d" % (
                    label, len(matches)))
        return matches[0]

    def _context_cold_pull_material(self):
        materials = tuple(getattr(self.host, "cold_pull_materials", ()))
        if self.material in materials:
            return self.material
        if not materials:
            raise RuntimeError("No cold-pull material profiles are available")
        return materials[0]

    def _install_nonpersistent_material_guard(self):
        params = getattr(self.host, "params", None)
        if params is None or "params_store" in self.context_runtime:
            return
        namespace = getattr(params, "__dict__", {})
        had_instance = "_store_value" in namespace
        instance_value = namespace.get("_store_value")
        original = params._store_value

        def store_value(param, value):
            if getattr(param, "key", None) != "current_material":
                return original(param, value)
            previous = params.variables[param.key]
            params.variables[param.key] = value
            return previous != value

        params._store_value = store_value
        self.context_runtime["params_store"] = (
            params, had_instance, instance_value)

    def _prepare_context_material(self):
        self._install_nonpersistent_material_guard()
        params = getattr(self.host, "params", None)
        if params is not None:
            self.context_runtime.setdefault(
                "current_material", params.variables.get("current_material"))
        self._context_cold_pull_material()

    def _prepare_context_print(self):
        self._install_nonpersistent_material_guard()
        params = getattr(self.host, "params", None)
        controlled = {
            "check_md5": 0,
            "disable_cleaning": False,
            "use_kamp": False,
            "print_leveling": False,
            "bed_mesh_validation": True,
            "bed_mesh_validation_clear": False,
            "disable_priming": True,
        }
        if params is not None:
            saved = {}
            for key, value in controlled.items():
                if key in params.variables:
                    saved[key] = params.variables[key]
                    params.variables[key] = value
            self.context_runtime["mod_params"] = saved
            self.context_runtime.setdefault(
                "current_material", params.variables.get("current_material"))

        client = self.host.printer.lookup_object(
            "gcode_macro _CLIENT_VARIABLE", None)
        if client is not None:
            variables = getattr(client, "variables", {})
            self.context_runtime["client_macro"] = client
            self.context_runtime["client_idle_timeout"] = variables.get(
                "idle_timeout")
            variables["idle_timeout"] = 2

        idle_timeout = getattr(self.host, "idle_timeout", None)
        self.context_runtime["idle_timeout"] = getattr(
            idle_timeout, "timeout", None)
        self._create_context_print_files()

    def _create_context_print_files(self):
        root = os.path.realpath(self.host.virtual_sdcard.sdcard_dirname)
        nozzle, bed = self.host._limited_preheat(self.material)
        safe_run = re.sub(r"[^a-zA-Z0-9_.-]", "-", self.run_id)
        paths = [
            os.path.join(root, "feather-context-%s-kamp.gcode" % safe_run),
            os.path.join(root, "feather-context-%s-recovery.gcode" % safe_run),
        ]
        common = (
            "; Feather operation-context runner fixture\n"
            "; Copyright (C) 2026, Alexander K <https://github.com/drA1ex>\n"
            "START_PRINT EXTRUDER_TEMP=%.0f BED_TEMP=%.0f %s\n"
            "G90\nG1 X0 Y0 Z5 F6000\n")
        payloads = [
            common % (nozzle, bed, "FORCE_KAMP=1")
            + "G4 P500\nEND_PRINT\n",
            common % (nozzle, bed, "")
            + "M83\nG1 E1 F300\n" + ("G4 P250\n" * 80) + "END_PRINT\n",
        ]
        self.context_files = []
        for path, payload in zip(paths, payloads):
            if os.path.exists(path):
                raise RuntimeError("Runner G-code path already exists: %s" % (
                    os.path.basename(path),))
            self.context_files.append(path)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

    def _open_context_file(self, path):
        path = os.path.realpath(path)
        root = os.path.realpath(self.host.virtual_sdcard.sdcard_dirname)
        if not os.path.isfile(path) or not path.startswith(root + os.sep):
            raise RuntimeError("Runner G-code file is unavailable")
        stat = os.stat(path)
        cache = getattr(self.host, "file_entry_cache", None)
        loaded_at = getattr(self.host, "file_entry_loaded_at", None)
        if (cache is not None and loaded_at is not None
                and "file_browser" not in self.context_runtime):
            self.context_runtime["file_browser"] = {
                "cache_present": "internal" in cache,
                "cache": cache.get("internal"),
                "loaded_present": "internal" in loaded_at,
                "loaded": loaded_at.get("internal"),
                "entries": getattr(self.host, "file_entries", None),
                "source": getattr(self.host, "file_source", "internal"),
                "page": getattr(self.host, "file_page", 0),
            }
        entry = {
            "name": os.path.basename(path), "path": path,
            "directory": False, "size": stat.st_size,
            "mtime": stat.st_mtime,
        }
        if cache is not None and loaded_at is not None:
            # Invalidate an older asynchronous scan and publish a runner-only
            # one-entry fixture through the normal browser rendering path.
            self.host.file_scan_token = getattr(
                self.host, "file_scan_token", 0) + 1
            self.host.file_scan_loading = False
            self.host.file_scan_source = None
            cache["internal"] = [entry]
            loaded_at["internal"] = self.reactor.monotonic()
        self.host.file_page = 0
        self.host.file_source = "internal"
        self.host.selected_file = None
        self.host.file_entries = [entry]
        self._show(Page.FILE_BROWSER)

    def _context_printing(self):
        state = str(self.host.print_stats.get_status(
            self.reactor.monotonic()).get("state", "")).lower()
        return state == "printing" and self.host.page == Page.PRINTING

    def _context_print_controls_ready(self):
        return (self._context_printing()
                and "print.pause" in self.host.renderer._buttons)

    def _context_paused(self):
        state = str(self.host.print_stats.get_status(
            self.reactor.monotonic()).get("state", "")).lower()
        return (state == "paused" and self.host.page == Page.PAUSED
                and "print.resume" in self.host.renderer._buttons)

    def _context_print_complete(self):
        state = str(self.host.print_stats.get_status(
            self.reactor.monotonic()).get("state", "")).lower()
        return (state not in ("printing", "paused")
                and not self.host.virtual_sdcard.is_active()
                and self.host.print_state == PrintState.IDLE)

    def _context_cancelled(self):
        state = str(self.host.print_stats.get_status(
            self.reactor.monotonic()).get("state", "")).lower()
        return (state == "cancelled"
                and not self.host.virtual_sdcard.is_active()
                and self.host.print_state == PrintState.IDLE
                and self.host.page == Page.MESSAGE
                and "message.ok" in self.host.renderer._buttons)

    def _context_idle_timeout(self):
        status = self.host.idle_timeout.get_status(
            self.reactor.monotonic())
        return str(status.get("state", "")).strip().lower() == "idle"

    def _context_checkpoint_ready(self):
        resurrection = getattr(self.host, "resurrection", None)
        path = getattr(resurrection, "file_path", None)
        if not path or not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as stream:
                checkpoint = json.load(stream)
        except (OSError, ValueError):
            return False
        source = os.path.realpath(str(checkpoint.get("file_path", "")))
        expected = set(os.path.realpath(path) for path in self.context_files)
        if source not in expected:
            raise RuntimeError("Recovery checkpoint belongs to another file")
        self.context_checkpoint = path
        return True

    def _cancel_context_print(self):
        if not self._context_checkpoint_ready():
            raise RuntimeError("Runner recovery checkpoint is unavailable")
        self.host.virtual_sdcard.do_cancel()

    def _activate_context_recovery(self):
        if not self._context_checkpoint_ready():
            raise RuntimeError("Runner recovery checkpoint is unavailable")
        resurrection = self.host.resurrection
        self.host._run_script("_CONTEXT_RESET")
        # A real restart presents recovery from an idle controller. Recreate
        # only that volatile controller fact so the subsequent restored print
        # gets the normal IDLE -> PRINTING page transition.
        self.host.print_state = PrintState.IDLE
        resurrection._pause_checkpoint_active = False
        resurrection._resume_pending = False
        state_type = type(resurrection.state)
        resurrection._change_state(state_type.RESURRECTION)
        for line in (
                "// action:prompt_begin Resurrection",
                "// action:prompt_text Resurrection is available! Would you like to restore the print?",
                "// action:prompt_footer_button Restore|RESURRECT",
                "// action:prompt_footer_button Cleanup|RESURRECT_ABORT",
                "// action:prompt_footer_button Later|RESPOND TYPE=command MSG=action:prompt_end",
                "// action:prompt_show"):
            resurrection.gcode.respond_raw(line)
        if self.host.page != Page.RECOVERY_PROMPT:
            raise RuntimeError("Recovery action prompt did not open")

    def _show(self, page):
        self.host._show_page(page)
        if self.host.page != page:
            raise RuntimeError("Unable to show page %s" % page.name)

    def _render_calibration_variants(self):
        calibration = self.host.feature_manager.get("calibration")
        if "cal.next" in self.host.renderer._buttons:
            calibration._handle_calibration_action("cal.next")

    def _open_calibration_home(self):
        calibration = self.host.feature_manager.get("calibration")
        # UI coverage intentionally leaves the paginated catalog on its next
        # page. Hardware phases must be independent from that presentation
        # state and the three tested entries all live on page zero.
        calibration.calibration_page = 0
        self._show(Page.CALIBRATION_HOME)

    def _open_safe_file_confirm(self):
        entries = list(getattr(self.host, "file_entries", ()))
        for index, entry in enumerate(entries[:5]):
            if entry["directory"]:
                continue
            action = "file.item%d" % index
            if action not in self.host.renderer._buttons:
                continue
            self.dispatching_test_action = True
            try:
                self.host._dispatch_action(action)
            finally:
                self.dispatching_test_action = False
            return

    def _return_from_file_confirm(self):
        if self.host.page == Page.FILE_CONFIRM:
            self.host._show_page(Page.FILE_BROWSER)

    def _pause_ui_timer(self):
        timer = getattr(self.host, "timer", None)
        self.original["timer_active"] = timer is not None
        if timer is None:
            return
        self.reactor.unregister_timer(timer)
        self.host.timer = None

    def _resume_ui_timer(self):
        if (not self.original.get("timer_active")
                or getattr(self.host, "timer", None) is not None):
            return
        self.host.timer = self.reactor.register_timer(
            self.host._update, self.reactor.NOW)

    def _render_safe_filament_action(self):
        self._render_safe_filament_snapshot(130.4, 250.0)

    def _render_safe_filament_cooling(self):
        self._render_safe_filament_snapshot(260.4, 250.0, update=True)

    def _render_safe_filament_snapshot(self, temperature, target,
                                       update=False):
        if not self.host.heating_materials:
            return
        self.host.filament_material = self.material or self.host.heating_materials[0]
        extruder = self.host.extruder

        class SnapshotExtruder:
            heater = extruder.heater
            min_extrude_temp = getattr(extruder, "min_extrude_temp", 170.0)

            def get_status(self, eventtime):
                status = dict(extruder.get_status(eventtime))
                status.update({
                    "temperature": float(temperature),
                    "target": float(target),
                })
                return status

        # Exercise deliberately wide heating/cooling states without touching
        # either heater or fan. Restoring the object keeps the test read-only.
        self.host.extruder = SnapshotExtruder()
        try:
            if update:
                if self.host.page != Page.FILAMENT_ACTION:
                    raise RuntimeError(
                        "Filament telemetry update requires action page")
                # Exercise the same declarative dirty-tree path used by live
                # HEATING -> COOLING transitions.  Reopening the current page
                # would clear the framebuffer and test navigation instead of
                # state updates.
                self.host.feature_manager.get("filament").update(
                    self.reactor.monotonic())
            else:
                self._show(Page.FILAMENT_ACTION)
        finally:
            self.host.extruder = extruder

    def _remember_ui_filament_target(self):
        self.ui_filament_target = float(self.host.extruder.get_status(
            self.reactor.monotonic()).get("target", 0.0))

    def _assert_ui_filament_target_preserved(self):
        target = float(self.host.extruder.get_status(
            self.reactor.monotonic()).get("target", 0.0))
        if target != self.ui_filament_target:
            raise RuntimeError(
                "Filament Back changed nozzle target: %.1f -> %.1f" %
                (self.ui_filament_target, target))

    def _dismiss_move_caution(self):
        buttons = self.host.renderer._buttons
        for action in (move_actions.CAUTION_AUTO.wire_id,
                       move_actions.CAUTION_DISMISS.wire_id,
                       move_actions.CAUTION_UNLOAD.wire_id):
            if action in buttons:
                # Caution actions do not move the toolhead. Use direct semantic
                # dispatch here so the next Home tap remains a separate step.
                self.dispatching_test_action = True
                try:
                    self.host._dispatch_action(action)
                finally:
                    self.dispatching_test_action = False
                break

    def _save_motion_origin(self):
        status = self.host.toolhead.get_status(self.reactor.monotonic())
        if not all(axis in str(status.get("homed_axes", "")) for axis in "xyz"):
            raise RuntimeError("Home All did not home XYZ")
        self.motion_origin = tuple(float(value) for value in status["position"][:3])
        self.host.jog_step = 1.0
        self.host._render_move()

    def _motion_step(self, axis, outward):
        status = self.host.toolhead.get_status(self.reactor.monotonic())
        index = "xyz".index(axis)
        current = float(status["position"][index])
        low, high = self.host._feather_move_limits(status)[index]
        if outward > 0:
            direction = 1 if high - current >= current - low else -1
            self.motion_expected = current + direction
        else:
            origin = self.motion_origin[index]
            # Homing may report a position a fraction beyond the configured UI
            # travel bound (for example Y=110.099675 with a 110.0 maximum).
            # MOVE_SAFE correctly clamps the return, so compare against that
            # reachable coordinate rather than the raw homing overshoot.
            target = max(low, min(high, origin))
            direction = 1 if target > current else -1
            self.motion_expected = target
        actions = {
            ("x", 1): move_actions.X_PLUS,
            ("x", -1): move_actions.X_MINUS,
            ("y", 1): move_actions.Y_PLUS,
            ("y", -1): move_actions.Y_MINUS,
            ("z", 1): move_actions.Z_PLUS,
            ("z", -1): move_actions.Z_MINUS,
        }
        action = actions[(axis, direction)].wire_id
        self.dispatching_test_action = True
        try:
            self.host._dispatch_action(action)
        finally:
            self.dispatching_test_action = False

    def _motion_reached(self, axis):
        index = "xyz".index(axis)
        actual = float(self.host.toolhead.get_status(
            self.reactor.monotonic())["position"][index])
        return math.isclose(
            actual, self.motion_expected, abs_tol=0.05)

    def _save_heat_initial(self):
        values = self._temperatures()
        self.heat_initial = (values["nozzle"], values["bed"])
        self.heat_stable_since = None

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
            self.worker.log("TEMPERATURE %s" % json.dumps(values, sort_keys=True))
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

    def _heat_targets_set(self):
        values = self._temperatures()
        nozzle, bed = self.host.heating_profiles[self.material]
        return (abs(values["nozzle_target"] - nozzle) <= 1.0
                and abs(values["bed_target"] - bed) <= 1.0)

    def _heat_is_rising(self):
        values = self._temperatures()
        return (values["nozzle"] >= self.heat_initial[0] + 3.0
                or abs(values["nozzle"] - values["nozzle_target"]) <= 5.0)

    def _heat_is_stable(self):
        values = self._temperatures()
        stable = (abs(values["nozzle"] - values["nozzle_target"]) <= 5.0
                  and abs(values["bed"] - values["bed_target"]) <= 3.0)
        now = self.reactor.monotonic()
        if not stable:
            self.heat_stable_since = None
            return False
        if self.heat_stable_since is None:
            self.heat_stable_since = now
        return now - self.heat_stable_since >= 10.0

    def _heaters_off(self):
        values = self._temperatures()
        return values["nozzle_target"] <= 0.0 and values["bed_target"] <= 0.0

    def _calibration_result(self):
        return self.host.page == Page.CALIBRATION_RESULT

    def _z_summary_ready(self):
        return self.host.page == Page.Z_OFFSET_SUMMARY

    def _z_paper_ready(self):
        return self.host.page == Page.Z_OFFSET_PAPER

    def _z_discarded(self):
        feature = self.host.feature_manager.get("z")
        return (self.host.page == Page.CALIBRATION_HOME
                and not feature.z_calibration.active)

    def _save_mesh_snapshot(self):
        mesh = self.host.bed_mesh
        status = mesh.get_status(self.reactor.monotonic())
        self._mesh_snapshot = (
            getattr(mesh, "z_mesh", None), str(status.get("profile_name", "") or ""))

    def _restore_mesh_snapshot(self):
        if self._mesh_snapshot is None:
            return
        feature = self.host.feature_manager.get("z")
        feature._restore_z_mesh(*self._mesh_snapshot)
        current = self.host.bed_mesh.get_status(
            self.reactor.monotonic()).get("profile_name", "")
        if str(current or "") != self._mesh_snapshot[1]:
            raise RuntimeError("Mesh profile was not restored")
        self._mesh_snapshot = None

    def _validate_mesh(self):
        calibration = self.host.feature_manager.get("calibration")
        matrix = calibration.calibration_mesh
        if not matrix or not matrix[0]:
            raise RuntimeError("Mesh result is empty")
        width = len(matrix[0])
        if any(len(row) != width for row in matrix):
            raise RuntimeError("Mesh result is not rectangular")
        if not all(math.isfinite(float(value))
                   for row in matrix for value in row):
            raise RuntimeError("Mesh contains a non-finite value")
        if "cal.mesh.discard" not in self.host.renderer._buttons:
            raise RuntimeError("Mesh result has no DON'T SAVE action")
        self.test_results["mesh"] = matrix

    def _dismiss_pressure(self):
        try:
            action = self._action_for_label("OK")
        except RuntimeError:
            return
        self.dispatching_test_action = True
        try:
            self.host._dispatch_action(action)
        finally:
            self.dispatching_test_action = False

    def _save_z_probe_position(self):
        feature = self.host.feature_manager.get("z")
        if not feature.z_calibration.ready_for_paper_test:
            raise RuntimeError("Z probe did not produce an adjustable result")
        self.z_probe_local = float(feature.z_calibration.local_z)
        self.test_results["z"] = {"post_probe_local_z": self.z_probe_local}

    def _verify_z_farther(self):
        current = float(self.host.feature_manager.get(
            "z").z_calibration.local_z)
        if not math.isclose(current, self.z_probe_local + 1.0, abs_tol=0.01):
            raise RuntimeError("Z FARTHER sequence did not create 1 mm clearance")
        self.test_results["z"]["farther_local_z"] = current

    def _verify_z_return(self):
        current = float(self.host.feature_manager.get(
            "z").z_calibration.local_z)
        if not math.isclose(current, self.z_probe_local, abs_tol=0.01):
            raise RuntimeError("Z CLOSER sequence did not return safely")
        self.test_results["z"]["returned_local_z"] = current

    def _verify_z_cleanup(self):
        feature = self.host.feature_manager.get("z")
        if feature.z_calibration.active:
            raise RuntimeError("Z calibration session remains active")
        current = float(self.host.gcode_move.get_status(
            self.reactor.monotonic())["homing_origin"][2])
        if not math.isclose(current, self.original["runtime_z"], abs_tol=0.001):
            raise RuntimeError("Runtime Z offset was not restored")

    def _temperature_wait_active(self):
        wait = getattr(self.host, "temperature_wait", None)
        return bool(wait is not None and getattr(
            wait, "variables", {}).get("active", False))

    def _hardware_cleanup(self):
        self.host._run_script("TURN_OFF_HEATERS")

    def _restore_context_runtime(self):
        first_error = None
        if self.suite == "CONTEXT_PRINT":
            try:
                virtual_sdcard = self.host.virtual_sdcard
                file_path = getattr(
                    virtual_sdcard, "file_path", lambda: None)()
                if virtual_sdcard.is_active() or file_path:
                    virtual_sdcard.do_cancel()
            except Exception as exc:
                first_error = exc
                logging.exception(
                    "[feather_ui_test] unable to cancel runner print")
        try:
            manager = getattr(self.host, "operation_context", None)
            if (manager is not None
                    and manager.get_status(
                        self.reactor.monotonic()).get("contexts")):
                self.host._run_script("_CONTEXT_RESET")
        except Exception as exc:
            if first_error is None:
                first_error = exc
            logging.exception(
                "[feather_ui_test] unable to reset operation contexts")

        params = getattr(self.host, "params", None)
        if params is not None:
            for key, value in self.context_runtime.get(
                    "mod_params", {}).items():
                params.variables[key] = value
            if "current_material" in self.context_runtime:
                params.variables["current_material"] = self.context_runtime[
                    "current_material"]
        guard = self.context_runtime.get("params_store")
        if guard is not None:
            guarded, had_instance, instance_value = guard
            if had_instance:
                guarded._store_value = instance_value
            else:
                try:
                    del guarded.__dict__["_store_value"]
                except (AttributeError, KeyError):
                    pass

        client = self.context_runtime.get("client_macro")
        if client is not None:
            client.variables["idle_timeout"] = self.context_runtime.get(
                "client_idle_timeout")
        timeout = self.context_runtime.get("idle_timeout")
        if timeout is not None:
            try:
                self.host._run_script(
                    "SET_IDLE_TIMEOUT TIMEOUT=%g" % float(timeout))
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                logging.exception(
                    "[feather_ui_test] unable to restore idle timeout")

        browser = self.context_runtime.get("file_browser")
        if browser is not None:
            cache = self.host.file_entry_cache
            loaded_at = self.host.file_entry_loaded_at
            if browser["cache_present"]:
                cache["internal"] = browser["cache"]
            else:
                cache.pop("internal", None)
            if browser["loaded_present"]:
                loaded_at["internal"] = browser["loaded"]
            else:
                loaded_at.pop("internal", None)
            self.host.file_entries = browser["entries"]
            self.host.file_source = browser["source"]
            self.host.file_page = browser["page"]

        owned_files = tuple(os.path.realpath(path)
                            for path in self.context_files)
        for path in tuple(self.context_files):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if first_error is None:
                    first_error = exc
                logging.exception(
                    "[feather_ui_test] unable to remove runner G-code")
        self.context_files = []
        resurrection = getattr(self.host, "resurrection", None)
        checkpoint = self.context_checkpoint
        candidate = getattr(resurrection, "file_path", None)
        if checkpoint is None and candidate and os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as stream:
                    saved = json.load(stream)
                source = os.path.realpath(str(saved.get("file_path", "")))
                if source in owned_files:
                    checkpoint = candidate
            except (OSError, ValueError):
                pass
        if (checkpoint and resurrection is not None
                and checkpoint == getattr(resurrection, "file_path", None)):
            try:
                os.unlink(checkpoint)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if first_error is None:
                    first_error = exc
                logging.exception(
                    "[feather_ui_test] unable to remove runner checkpoint")
            resurrection._checkpoint_cache = None
            resurrection._checkpoint_cache_loaded = False
            resurrection._pause_checkpoint_active = False
            resurrection._resume_pending = False
            state = getattr(resurrection, "state", None)
            state_type = type(state)
            if (hasattr(state_type, "IDLE")
                    and hasattr(resurrection, "_change_state")):
                resurrection._change_state(state_type.IDLE)
        self.context_checkpoint = None
        self.context_runtime = {}
        if first_error is not None:
            raise first_error

    def _restore_state(self):
        first_error = None
        try:
            self._restore_context_runtime()
        except Exception as exc:
            first_error = exc
        if self.suite not in NONPHYSICAL_SUITES:
            try:
                z = self.host.feature_manager.peek("z")
                if z is not None and z.z_calibration.active:
                    z._cancel_z_calibration()
            except Exception as exc:
                first_error = exc
                logging.exception(
                    "[feather_ui_test] unable to cancel Z session")
            try:
                self.host._run_script("TURN_OFF_HEATERS")
                self.host._run_script(
                    "M104 S%.1f\nM140 S%.1f" % (
                        self.original.get("extruder_target", 0.0),
                        self.original.get("bed_target", 0.0)))
                if getattr(self.host, "fan", None) is not None:
                    self.host._run_script(
                        "SET_FAN_SPEED FAN=fanM106 SPEED=%.4f" %
                        self.original.get("fan_speed", 0.0))
                self.host._run_script("_SET_GCODE_OFFSET Z=%+.6f MOVE=0" %
                                      self.original["runtime_z"])
                feature = self.host.feature_manager.get("z")
                feature._restore_z_mesh(
                    self.original["mesh_object"],
                    self.original["mesh_profile"])
                self.host._run_script("M84")
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                logging.exception("[feather_ui_test] cleanup failed")
        self.host.filament_material = self.original["filament_material"]
        try:
            self._resume_ui_timer()
        except Exception as exc:
            if first_error is None:
                first_error = exc
        try:
            self.host._show_page(self.original["page"])
            self.host.previous_page = self.original["previous_page"]
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

    def _event(self, message):
        logging.info("[feather_ui_test] %s", message)
        if self.worker is not None:
            self.worker.log(message)

    def deactivate(self):
        self.abort_requested = True
        if self.context_recorder is not None:
            self.context_recorder.abort_active("feature deactivated")
            self.context_recorder.detach()
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        self.running = False
