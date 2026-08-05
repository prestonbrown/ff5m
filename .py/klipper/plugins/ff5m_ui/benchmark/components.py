"""Feature-local declarative components for the render benchmark."""

import math

from ui import ThemeColor
from ui.bindings import resolve
from ui.components import Component


class TextCube(Component):
    """Render a rotating wireframe cube from fixed-width text rows."""

    covers_bounds = True
    _COLUMNS = 39
    _ROWS = 13
    _EDGES = (
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    )
    _VERTICES = (
        (-1.0, -1.0, -1.0), (1.0, -1.0, -1.0),
        (1.0, 1.0, -1.0), (-1.0, 1.0, -1.0),
        (-1.0, -1.0, 1.0), (1.0, -1.0, 1.0),
        (1.0, 1.0, 1.0), (-1.0, 1.0, 1.0),
    )
    _GLYPHS = "FEATHERX"
    _PALETTES = (
        (ThemeColor.PRIMARY, ThemeColor.SECONDARY, ThemeColor.BRIGHT),
        (ThemeColor.SECONDARY, ThemeColor.SUCCESS, ThemeColor.BRIGHT),
        (ThemeColor.PRIMARY, ThemeColor.WARNING, ThemeColor.BRIGHT),
    )

    def __init__(self, angle_x, angle_y, angle_z, palette_phase, key=None):
        super().__init__(key=key)
        self.angle_x = angle_x
        self.angle_y = angle_y
        self.angle_z = angle_z
        self.palette_phase = palette_phase

    @staticmethod
    def _rotate(vertex, angle_x, angle_y, angle_z):
        x, y, z = vertex
        cx, sx = math.cos(angle_x), math.sin(angle_x)
        cy, sy = math.cos(angle_y), math.sin(angle_y)
        cz, sz = math.cos(angle_z), math.sin(angle_z)

        y, z = y * cx - z * sx, y * sx + z * cx
        x, z = x * cy + z * sy, -x * sy + z * cy
        x, y = x * cz - y * sz, x * sz + y * cz
        return x, y, z

    @classmethod
    def _project(cls, vertex):
        x, y, z = vertex
        perspective = 3.0 / (4.3 - z)
        return (
            (cls._COLUMNS - 1) * 0.5 + x * perspective * 10.2,
            (cls._ROWS - 1) * 0.5 + y * perspective * 5.2,
            z,
        )

    @classmethod
    def _rows(cls, angle_x, angle_y, angle_z):
        projected = tuple(cls._project(cls._rotate(
            vertex, angle_x, angle_y, angle_z))
            for vertex in cls._VERTICES)
        depth = [[-1000.0] * cls._COLUMNS for _ in range(cls._ROWS)]
        glyph = [[" "] * cls._COLUMNS for _ in range(cls._ROWS)]

        for edge_index, (start_index, end_index) in enumerate(cls._EDGES):
            start = projected[start_index]
            end = projected[end_index]
            span = max(abs(end[0] - start[0]), abs(end[1] - start[1]))
            steps = max(2, int(math.ceil(span * 2.0)))
            for step in range(steps + 1):
                ratio = step / float(steps)
                column = int(round(
                    start[0] + (end[0] - start[0]) * ratio))
                row = int(round(
                    start[1] + (end[1] - start[1]) * ratio))
                value_depth = start[2] + (end[2] - start[2]) * ratio
                if not (0 <= column < cls._COLUMNS
                        and 0 <= row < cls._ROWS):
                    continue
                if value_depth < depth[row][column]:
                    continue
                depth[row][column] = value_depth
                value = cls._GLYPHS[
                    (edge_index + step) % len(cls._GLYPHS)]
                glyph[row][column] = (
                    value if value_depth >= 0.0 else value.lower())

        for x, y, z in projected:
            column, row = int(round(x)), int(round(y))
            if 0 <= column < cls._COLUMNS and 0 <= row < cls._ROWS:
                depth[row][column] = max(depth[row][column], z + 0.01)
                glyph[row][column] = "+"

        occupied = {
            (column, row)
            for row in range(cls._ROWS)
            for column in range(cls._COLUMNS)
            if glyph[row][column] != " "
        }
        shadow = [[" "] * cls._COLUMNS for _ in range(cls._ROWS)]
        for column, row in occupied:
            shadow_column, shadow_row = column + 2, row + 1
            if (0 <= shadow_column < cls._COLUMNS
                    and 0 <= shadow_row < cls._ROWS
                    and (shadow_column, shadow_row) not in occupied):
                shadow[shadow_row][shadow_column] = ":"

        rows = []
        for row in range(cls._ROWS):
            text = "".join(glyph[row]).rstrip()
            shadow_text = "".join(shadow[row]).rstrip()
            visible_depths = tuple(
                depth[row][column]
                for column in range(cls._COLUMNS)
                if glyph[row][column] != " ")
            average_depth = (sum(visible_depths) / len(visible_depths)
                             if visible_depths else -1000.0)
            rows.append((text, shadow_text, average_depth))
        return tuple(rows)

    @staticmethod
    def _row_color(palette, depth, row):
        if depth < -0.2:
            return palette[0]
        if depth > 0.35:
            return palette[2]
        return palette[1] if row % 2 else palette[0]

    def draw(self, renderer, state, bounds):
        angle_x = float(resolve(self.angle_x, state))
        angle_y = float(resolve(self.angle_y, state))
        angle_z = float(resolve(self.angle_z, state))
        phase = int(resolve(self.palette_phase, state)) % len(self._PALETTES)
        rows = self._rows(angle_x, angle_y, angle_z)
        palette = self._PALETTES[phase]
        x, y, width, height = bounds
        advance_x = renderer.font_advance("JetBrainsMono 8pt")
        line_height = 21
        text_width = self._COLUMNS * advance_x
        text_height = self._ROWS * line_height
        text_x = x + max(8, (width - text_width) // 2)
        text_y = y + max(8, (height - text_height) // 2)
        commands = [
            renderer.fill(x, y, width, height, ThemeColor.BACKGROUND),
            renderer.stroke(x, y, width, height, ThemeColor.BORDER, 1),
        ]
        for row, (text, shadow, depth) in enumerate(rows):
            row_y = text_y + row * line_height
            if shadow:
                commands.append(renderer.text(
                    text_x, row_y, shadow, ThemeColor.MUTED,
                    "JetBrainsMono 8pt", "left", "top"))
            if text:
                commands.append(renderer.text(
                    text_x, row_y, text,
                    self._row_color(palette, depth, row),
                    "JetBrainsMono Bold 8pt", "left", "top"))
        return commands


class BenchmarkStats(Component):
    """Compact fixed-cost statistics panel updated below animation rate."""

    covers_bounds = True
    _LABELS = (
        "COMMIT FPS\n"
        "FRAME MED\n"
        "FRAME P95\n"
        "TYPER\n"
        "FLUSH\n"
        "PYTHON\n"
        "MISSED"
    )

    def __init__(self, values, status, key=None):
        super().__init__(key=key)
        self.values = values
        self.status = status

    def draw(self, renderer, state, bounds):
        values = str(resolve(self.values, state))
        status = str(resolve(self.status, state))
        status_color = (ThemeColor.WARNING if status.startswith((
            "WARMUP", "TIMEOUT", "FAILED", "QUEUE"))
            else ThemeColor.SUCCESS)
        x, y, width, height = bounds
        top = y + 24
        commands = [
            renderer.fill(x, y, width, height, ThemeColor.PANEL),
            renderer.stroke(x, y, width, height, ThemeColor.BORDER, 1),
        ]
        labels = self._LABELS.splitlines()
        value_lines = values.splitlines()
        for index, label in enumerate(labels):
            line_y = top + index * 35
            commands.append(renderer.text(
                x + 14, line_y, label, ThemeColor.DIM,
                "JetBrainsMono 8pt", "left", "top"))
            value = value_lines[index] if index < len(value_lines) else "--"
            commands.append(renderer.text(
                x + width - 14, line_y, value, ThemeColor.BRIGHT,
                "JetBrainsMono Bold 8pt", "right", "top"))
        commands.append(renderer.text(
            x + width // 2, y + height - 24, status, status_color,
            "JetBrainsMono Bold 8pt", "center", "middle",
            max_width=width - 24, truncate=True))
        return commands
