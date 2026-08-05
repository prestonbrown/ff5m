"""Declarative render benchmark page."""

from ui import FLEX, Grid, PageTree, Rect
from ui.bindings import bind

from ..keys import AppPage
from .components import BenchmarkStats, TextCube
from .state import BenchmarkState


CONTENT = Rect(18, 56, 764, 386)


def create_page():
    root = Grid(
        matrix=((
            TextCube(
                bind(BenchmarkState.ANGLE_X),
                bind(BenchmarkState.ANGLE_Y),
                bind(BenchmarkState.ANGLE_Z),
                bind(BenchmarkState.PALETTE_PHASE),
            ).repaint_boundary(),
            BenchmarkStats(
                bind(BenchmarkState.VALUES),
                bind(BenchmarkState.STATUS),
            ).repaint_boundary(),
        ),),
        columns=(FLEX, 210), rows=(FLEX,), gap=10,
    ).padding(left=8, top=8, right=8, bottom=8)
    return PageTree(root, CONTENT, page_id=AppPage.RENDER_BENCHMARK)
