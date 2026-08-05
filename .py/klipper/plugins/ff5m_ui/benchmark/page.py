"""Declarative render benchmark page."""

from ui import FLEX, Grid, Hitbox, Overlay, PageTree, Rect
from ui.bindings import bind

from ..keys import AppPage
from .actions import NEXT_MODE
from .components import BenchmarkStats, TextCube
from .state import BenchmarkState


CONTENT = Rect(18, 56, 764, 386)
CUBE_WIDTH = 460
CUBE_HEIGHT = 300


def create_page():
    cube_surface = Overlay(
        TextCube(
            bind(BenchmarkState.ANGLE_X),
            bind(BenchmarkState.ANGLE_Y),
            bind(BenchmarkState.ANGLE_Z),
            bind(BenchmarkState.PALETTE_PHASE),
            bind(BenchmarkState.MODE),
        ).size(CUBE_WIDTH, CUBE_HEIGHT) \
            .align(horizontal="center", vertical="center") \
            .repaint_boundary(),
        Hitbox(NEXT_MODE),
    )
    root = Grid(
        matrix=((
            cube_surface,
            BenchmarkStats(
                bind(BenchmarkState.VALUES),
                bind(BenchmarkState.MODE),
                bind(BenchmarkState.STATUS),
            ).repaint_boundary(),
        ),),
        columns=(FLEX, 210), rows=(FLEX,), gap=10,
    ).padding(left=8, top=8, right=8, bottom=8)
    return PageTree(root, CONTENT, page_id=AppPage.RENDER_BENCHMARK)
