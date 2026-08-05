"""Lazy interactive framebuffer benchmark for Feather."""

import math
import statistics
import time
from collections import deque

from ui import Page, PrintState, ReceiptTracker
from ui.lazy import LazyModule
from feather_feature_manager import FeatureHostProxy
from ff5m_ui.benchmark.actions import BenchmarkAction, BenchmarkRoute


benchmark_page = LazyModule("ff5m_ui.benchmark.page")
benchmark_state = LazyModule("ff5m_ui.benchmark.state")


class BenchmarkFeature(FeatureHostProxy):
    name = "benchmark"

    TARGET_FPS = 60.0
    FRAME_INTERVAL = 1.0 / TARGET_FPS
    RECEIPT_TIMEOUT = 1.0
    WARMUP_FRAMES = 30
    WINDOW_FRAMES = 120
    STATS_INTERVAL = 6
    MODES = ("text", "lines", "dots")
    def __init__(self, host):
        super().__init__(host)
        self.timer = None
        self.active = False
        self.page_tree = None
        self.tracker = ReceiptTracker(self.RECEIPT_TIMEOUT)
        self.session = 0
        self.frame = 0
        self.session_started = 0.0
        self.next_target = 0.0
        self.samples = deque(maxlen=self.WINDOW_FRAMES)
        self.receipt_times = deque(maxlen=self.WINDOW_FRAMES * 2)
        self.mode = self.MODES[0]
        self.display_values = "--\n--\n--\n--\n--\n--\n--"
        self.display_status = self._warmup_status()
        self.last_angles = (0.0, 0.0, 0.0)

    def initialize(self):
        self.timer = self.reactor.register_timer(
            self._tick, self.reactor.NEVER)

    def render(self, page):
        if page != Page.RENDER_BENCHMARK:
            raise ValueError("benchmark feature cannot render %s" % page)
        if self.print_state != PrintState.IDLE:
            raise RuntimeError("Render benchmark requires an idle printer")
        self.mode = self.MODES[0]
        self.page_tree = benchmark_page.create_page()
        self.active = True
        now = self.reactor.monotonic()
        self._reset_measurements(now)
        self._submit_frame(now, full=True)

    def allows_action(self, page, action):
        return page == Page.RENDER_BENCHMARK and action == "nav.back"

    def handle_action(self, page, action):
        return False

    def resolve_semantic_action(self, page, wire_id):
        if page != Page.RENDER_BENCHMARK or self.page_tree is None:
            return None
        return self.page_tree.resolve_action(wire_id)

    def handle_semantic_action(self, page, action):
        if page != Page.RENDER_BENCHMARK:
            return False
        if (isinstance(action, BenchmarkAction)
                and action.route == BenchmarkRoute.NEXT_MODE):
            self._cycle_mode(self.reactor.monotonic())
            return True
        raise KeyError("Unsupported benchmark action: %s" % action)

    def back(self, page):
        if page != Page.RENDER_BENCHMARK:
            return False
        self._stop_session()
        self._show_page(Page.SETTINGS)
        return True

    def update(self, eventtime):
        if self.active and self.print_state != PrintState.IDLE:
            self._stop_session()
            self._show_page(self.page_for_print_state())

    def on_page_changed(self, old_page, new_page):
        if old_page == Page.RENDER_BENCHMARK and new_page != old_page:
            self._stop_session()

    def on_print_state_changed(self, old_state, new_state, stats_state):
        if self.active and new_state != PrintState.IDLE:
            self._stop_session()

    def on_renderer_restarted(self):
        if self.page == Page.RENDER_BENCHMARK:
            self._stop_session()

    def on_render_receipt(self, receipt, eventtime):
        if not self.active:
            return
        measurement = self.tracker.resolve(receipt, eventtime)
        if measurement is None:
            return
        if not receipt.success:
            self._fail("FAILED RECEIPT")
            return

        metadata = measurement.metadata or {}
        sample = {
            "received_at": float(eventtime),
            "latency_ms": measurement.latency_ms,
            "typer_ms": receipt.total_us / 1000.0,
            "cpu_ms": receipt.cpu_us / 1000.0,
            "flush_ms": receipt.flush_us / 1000.0,
            "python_ms": float(metadata.get("python_ms", 0.0)),
        }
        self.receipt_times.append(float(eventtime))
        completed = self.frame
        if completed > self.WARMUP_FRAMES:
            self.samples.append(sample)
        if (completed <= self.WARMUP_FRAMES
                or completed % self.STATS_INTERVAL == 0):
            self._refresh_display(eventtime)

        if eventtime >= self.next_target:
            self.next_target = float(eventtime)
            waketime = float(eventtime)
        else:
            waketime = self.next_target
        self.reactor.update_timer(self.timer, waketime)

    def deactivate(self):
        self._stop_session()
        if self.timer is not None:
            try:
                self.reactor.unregister_timer(self.timer)
            except Exception:
                pass
            self.timer = None

    def _warmup_status(self):
        return "WARMUP %d/%d" % (
            min(self.frame, self.WARMUP_FRAMES), self.WARMUP_FRAMES)

    @staticmethod
    def _live_status():
        return "LIVE / 60 FPS"

    def _reset_measurements(self, eventtime):
        self.tracker.cancel()
        self.session += 1
        self.frame = 0
        self.session_started = float(eventtime)
        self.next_target = float(eventtime)
        self.samples.clear()
        self.receipt_times.clear()
        self.display_values = "--\n--\n--\n--\n--\n--\n--"
        self.display_status = self._warmup_status()
        self.last_angles = (0.0, 0.0, 0.0)

    def _stop_session(self):
        self.active = False
        self.tracker.cancel()
        self.samples.clear()
        self.receipt_times.clear()
        self.page_tree = None
        if self.timer is not None:
            try:
                self.reactor.update_timer(self.timer, self.reactor.NEVER)
            except Exception:
                pass

    def _state(self, eventtime):
        elapsed = max(0.0, float(eventtime) - self.session_started)
        angles = (
            elapsed * 0.83,
            elapsed * 1.17,
            elapsed * 0.29,
        )
        self.last_angles = angles
        state = benchmark_state.BenchmarkState
        return {
            state.ANGLE_X: angles[0],
            state.ANGLE_Y: angles[1],
            state.ANGLE_Z: angles[2],
            state.PALETTE_PHASE: int(elapsed // 2.5),
            state.MODE: self.mode,
            state.VALUES: self.display_values,
            state.STATUS: self.display_status,
        }

    def _token(self):
        return "%x:%d" % (self.session, self.frame + 1)

    def _submit_frame(self, eventtime, full=False):
        if not self.active or self.page != Page.RENDER_BENCHMARK:
            return self.reactor.NEVER
        if self.tracker.pending is not None:
            return self.tracker.pending.deadline

        build_started = time.perf_counter()
        state = self._state(eventtime)
        if full:
            commands = self.renderer.begin_page(
                "Render benchmark", back=True)
            commands += self.page_tree.draw(self.renderer, state)
            kind, key = "surface", None
        else:
            commands = self.page_tree.update(self.renderer, state)
            kind, key = "animation", "render-benchmark"
        python_ms = (time.perf_counter() - build_started) * 1000.0
        token = self._token()
        submitted_at = self.reactor.monotonic()
        self.tracker.expect(token, submitted_at, {"python_ms": python_ms})
        accepted = self.renderer.send(
            commands, kind=kind, key=key, receipt=token)
        if not accepted:
            self.tracker.cancel()
            self.display_status = "QUEUE BUSY"
            self.reactor.update_timer(
                self.timer, submitted_at + self.FRAME_INTERVAL)
            return submitted_at + self.FRAME_INTERVAL

        self.frame += 1
        self.next_target = max(
            self.next_target, submitted_at) + self.FRAME_INTERVAL
        deadline = self.tracker.pending.deadline
        self.reactor.update_timer(self.timer, deadline)
        return deadline

    def _tick(self, eventtime):
        if not self.active:
            return self.reactor.NEVER
        if self.tracker.expired(eventtime):
            self._fail("RECEIPT TIMEOUT")
            return self.reactor.NEVER
        if self.tracker.pending is not None:
            return self.tracker.pending.deadline
        self._submit_frame(eventtime)
        return (self.tracker.pending.deadline
                if self.tracker.pending is not None else
                eventtime + self.FRAME_INTERVAL)

    @staticmethod
    def _percentile(values, percentile):
        ordered = sorted(values)
        if not ordered:
            return 0.0
        index = int(math.ceil(percentile * len(ordered))) - 1
        return ordered[max(0, min(len(ordered) - 1, index))]

    def _refresh_display(self, eventtime):
        completed = self.frame
        if completed <= self.WARMUP_FRAMES:
            self.display_status = self._warmup_status()
            return
        samples = tuple(self.samples)
        if not samples:
            return
        recent_receipts = tuple(
            value for value in self.receipt_times
            if value >= float(eventtime) - 1.0)
        latency = tuple(item["latency_ms"] for item in samples)
        missed = 100.0 * sum(
            value > self.FRAME_INTERVAL * 1000.0 for value in latency
        ) / len(latency)
        self.display_values = (
            "%5.1f\n%5.1f MS\n%5.1f MS\n%5.1f MS\n"
            "%5.2f MS\n%5.2f MS\n%5.1f%%" % (
                len(recent_receipts),
                statistics.median(latency),
                self._percentile(latency, 0.95),
                statistics.median(
                    item["typer_ms"] for item in samples),
                statistics.median(
                    item["flush_ms"] for item in samples),
                statistics.median(
                    item["python_ms"] for item in samples),
                missed,
            ))
        self.display_status = self._live_status()

    def _cycle_mode(self, eventtime):
        if not self.active or self.page != Page.RENDER_BENCHMARK:
            return
        index = self.MODES.index(self.mode)
        self.mode = self.MODES[(index + 1) % len(self.MODES)]
        self._reset_measurements(eventtime)
        self._submit_frame(eventtime, full=True)

    def _fail(self, status):
        self.tracker.cancel()
        self.active = False
        self.display_status = str(status)
        if self.timer is not None:
            self.reactor.update_timer(self.timer, self.reactor.NEVER)
        page_tree = self.page_tree
        if page_tree is None or self.page != Page.RENDER_BENCHMARK:
            return
        state = benchmark_state.BenchmarkState
        commands = page_tree.update(self.renderer, {
            state.MODE: self.mode,
            state.VALUES: self.display_values,
            state.STATUS: self.display_status,
        })
        self.renderer.send_animation(commands, "render-benchmark-error")
