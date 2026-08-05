"""Typed state for the render benchmark page."""

from ui.bindings import state
from ui.identity import StateKey


class BenchmarkState(StateKey):
    __key_namespace__ = "ui.pages.benchmark.state.BenchmarkState"

    ANGLE_X = state(float, default=0.0)
    ANGLE_Y = state(float, default=0.0)
    ANGLE_Z = state(float, default=0.0)
    PALETTE_PHASE = state(int, default=0, minimum=0)
    MODE = state(str, default="text")
    VALUES = state(str, default="--\n--\n--\n--\n--\n--\n--")
    STATUS = state(str, default="WARMUP 0/30")
