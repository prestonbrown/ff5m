## Suite policy and scenario-local state for Feather UI test runs.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import base64
import binascii
import importlib
import json
import math
import os
import re

from ui import Page, PrintState
from ff5m_ui.move import actions as move_actions
from ff5m_ui.z_offset import actions as z_actions


MOTION_STEP_TIMEOUT = 10.0
MOTION_STEP_INTERVAL = 0.1
COMPONENT_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
MAX_COMPONENT_CASE_BYTES = 32 * 1024
MAX_COMPONENT_CASES = 64


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


class ScenarioCatalog:
    """Build fixed suites and own only their scenario-local mutable state."""

    def __init__(self, run):
        self.run = run
        self.motion_origin = None
        self.motion_expected = None
        self.heat_initial = None
        self.heat_stable_since = None
        self.mesh_snapshot = None
        self.ui_filament_target = None
        self.z_probe_local = None

    @property
    def host(self):
        return self.run.host

    @property
    def reactor(self):
        return self.run.reactor

    @property
    def material(self):
        return self.run.material

    @property
    def component_cases(self):
        return self.run.component_cases

    @property
    def context_fixture(self):
        return self.run.context_fixture

    @property
    def snapshot(self):
        return self.run.snapshot

    @property
    def test_results(self):
        return self.run.test_results

    def _start_context_scenario(self, name, fixtures):
        self.run._start_context_scenario(name, fixtures)

    def _finish_context_scenario(self):
        self.run._finish_context_scenario()

    def build_steps(self, suite):
        steps = []
        self._add_capture(steps, "baseline")
        steps[-1]["phase"] = "baseline"
        phases = (["ui", "render", "motion", "heat", "screws", "mesh", "z"]
                  if suite == "FULL" else [suite.lower()])
        builders = {
            "ui": self._steps_ui,
            "component": self._steps_component,
            "render": self._steps_render,
            "motion": self._steps_motion,
            "heat": self._steps_heat,
            "screws": self._steps_screws,
            "mesh": self._steps_mesh,
            "z": self._steps_z,
            "context_print": self._steps_context_print,
            "context_material": self._steps_context_material,
        }
        for phase in phases:
            first = len(steps)
            if phase in ("context_print", "context_material"):
                builders[phase](steps)
            else:
                fixtures = {
                    "screws": ("screws",),
                    "mesh": ("mesh_clean", "mesh_skip_clean"),
                    "z": ("z_offset_skip_clean",),
                }.get(phase, ("none",))
                self._add_call(
                    steps, "%s-context-start" % phase,
                    lambda name=phase, choices=fixtures:
                    self._start_context_scenario(name, choices), delay=0.0)
                builders[phase](steps)
                self._add_call(
                    steps, "%s-context-verify" % phase,
                    self._finish_context_scenario, delay=0.0)
            for step in steps[first:]:
                step["phase"] = phase
                if phase == "context_print":
                    for prefix, subphase in (
                            ("print_mesh-", "print_mesh"),
                            ("recovery-", "recovery"),
                            ("print_kamp-", "print_kamp")):
                        if step["label"].startswith(prefix):
                            step["phase"] = subphase
                            break
                elif phase == "context_material":
                    for prefix, subphase in (
                            ("filament-", "filament"),
                            ("cold_pull-", "cold_pull")):
                        if step["label"].startswith(prefix):
                            step["phase"] = subphase
                            break
        return steps

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
            lambda: self._open_context_file(self.context_fixture.files[1]))
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
            lambda: self._open_context_file(self.context_fixture.files[0]))
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

    def _prepare_context_material(self):
        self.context_fixture.prepare_material()
        self._context_cold_pull_material()

    def _prepare_context_print(self):
        self.context_fixture.prepare_print()

    def _open_context_file(self, path):
        self.context_fixture.open_file(
            path, self._show, Page.FILE_BROWSER)

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
                and self.host.print_state == PrintState.IDLE
                and self.host.page == Page.MESSAGE
                and "message.ok" in self.host.renderer._buttons)

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
        return self.context_fixture.checkpoint_ready()

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
            self.run._tap(action)
            return

    def _return_from_file_confirm(self):
        if self.host.page == Page.FILE_CONFIRM:
            self.host._show_page(Page.FILE_BROWSER)

    def _pause_ui_timer(self):
        timer = getattr(self.host, "timer", None)
        if timer is None:
            return
        self.reactor.unregister_timer(timer)
        self.host.timer = None

    def _resume_ui_timer(self):
        if (self.snapshot is None or not self.snapshot.timer_active
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
                # Keep synthetic input on the same feedback and dispatch path
                # as a real touch. The following Home tap is a separate step.
                self.run._tap(action)
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
        self.run._tap(action)

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
        self.run._tap(action)

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
        if not math.isclose(current, self.snapshot.runtime_z, abs_tol=0.001):
            raise RuntimeError("Runtime Z offset was not restored")

    def _hardware_cleanup(self):
        self.host._run_script("TURN_OFF_HEATERS")
