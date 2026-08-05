"""Feature-local declarative components for the render benchmark."""

import math

from ui import ThemeColor
from ui.bindings import resolve
from ui.components import Component


class TextCube(Component):
    """Render a rotating cube in one of several benchmark-specific modes."""

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
    # Keep animated primitives away from the repaint-boundary border.  The
    # asymmetric vertical inset also moves the cube slightly down: the text
    # raster extends farther above its anchor than below it on the device.
    _CONTENT_INSETS = (8, 12, 8, 4)
    _PALETTES = (
        (ThemeColor.PRIMARY, ThemeColor.SECONDARY, ThemeColor.BRIGHT),
        (ThemeColor.SECONDARY, ThemeColor.SUCCESS, ThemeColor.BRIGHT),
        (ThemeColor.PRIMARY, ThemeColor.WARNING, ThemeColor.BRIGHT),
    )

    def __init__(self, angle_x, angle_y, angle_z, palette_phase, mode,
                 key=None):
        super().__init__(key=key)
        self.angle_x = angle_x
        self.angle_y = angle_y
        self.angle_z = angle_z
        self.palette_phase = palette_phase
        self.mode = mode

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
    def _projected_vertices(cls, angle_x, angle_y, angle_z):
        return tuple(cls._project(cls._rotate(
            vertex, angle_x, angle_y, angle_z))
            for vertex in cls._VERTICES)

    @classmethod
    def _content_bounds(cls, bounds):
        x, y, width, height = bounds
        left, top, right, bottom = cls._CONTENT_INSETS
        return (
            x + left, y + top,
            max(1, width - left - right),
            max(1, height - top - bottom),
        )

    @classmethod
    def _rows(cls, projected):
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

    @staticmethod
    def _edge_color(palette, depth, edge_index):
        if depth < -0.1:
            return palette[0]
        if depth > 0.35:
            return palette[2]
        return palette[1] if edge_index % 2 else palette[0]

    @staticmethod
    def _line(renderer, start_x, start_y, end_x, end_y, color, line_width=1):
        return "--batch line -s %d %d -e %d %d -c %s -lw %d" % (
            int(round(start_x)), int(round(start_y)),
            int(round(end_x)), int(round(end_y)),
            renderer.color(color), int(line_width))

    @classmethod
    def _edge_samples(cls, start, end, steps=8):
        steps = max(2, int(steps))
        for step in range(steps + 1):
            ratio = step / float(steps)
            yield (
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
                start[2] + (end[2] - start[2]) * ratio,
            )

    @classmethod
    def _draw_text_mode(cls, renderer, bounds, projected, palette):
        x, y, width, height = bounds
        advance_x = max(
            renderer.font_advance("JetBrainsMono 8pt"),
            renderer.font_advance("JetBrainsMono Bold 8pt"),
        )
        line_height = 21
        text_width = cls._COLUMNS * advance_x
        text_height = cls._ROWS * line_height
        text_x = x + max(8, (width - text_width) // 2)
        text_y = y + max(8, (height - text_height) // 2)
        commands = []
        for row, (text, shadow, depth) in enumerate(cls._rows(projected)):
            row_y = text_y + row * line_height
            if shadow:
                commands.append(renderer.text(
                    text_x, row_y, shadow, ThemeColor.MUTED,
                    "JetBrainsMono 8pt", "left", "top"))
            if text:
                commands.append(renderer.text(
                    text_x, row_y, text,
                    cls._row_color(palette, depth, row),
                    "JetBrainsMono Bold 8pt", "left", "top"))
        return commands

    @classmethod
    def _draw_line_mode(cls, renderer, bounds, projected, palette):
        x, y, width, height = bounds
        center_x = x + width * 0.5
        center_y = y + height * 0.5
        scale_x = max(1.0, width / float(cls._COLUMNS + 6))
        scale_y = max(1.0, height / float(cls._ROWS + 5))

        def canvas(point):
            return (
                center_x + (point[0] - (cls._COLUMNS - 1) * 0.5) * scale_x,
                center_y + (point[1] - (cls._ROWS - 1) * 0.5) * scale_y,
                point[2],
            )

        points = tuple(canvas(point) for point in projected)
        commands = []
        edges = sorted(
            enumerate(cls._EDGES),
            key=lambda item: (points[item[1][0]][2] + points[item[1][1]][2]) * 0.5)
        for edge_index, (start_index, end_index) in edges:
            start = points[start_index]
            end = points[end_index]
            depth = (start[2] + end[2]) * 0.5
            color = cls._edge_color(palette, depth, edge_index)
            commands.append(cls._line(
                renderer, start[0] + 2, start[1] + 1,
                end[0] + 2, end[1] + 1, ThemeColor.MUTED, 1))
            commands.append(cls._line(
                renderer, start[0], start[1], end[0], end[1], color,
                2 if depth > 0.25 else 1))
        for point_x, point_y, depth in points:
            commands.append(renderer.fill(
                int(round(point_x)) + 1, int(round(point_y)), 3, 3,
                ThemeColor.MUTED))
            size = 3 if depth > 0.2 else 2
            commands.append(renderer.fill(
                int(round(point_x)) - size // 2,
                int(round(point_y)) - size // 2, size, size,
                palette[2] if depth > 0.2 else palette[1]))
        return commands

    @classmethod
    def _draw_dots_mode(cls, renderer, bounds, projected, palette):
        x, y, width, height = bounds
        center_x = x + width * 0.5
        center_y = y + height * 0.5
        scale_x = max(1.0, width / float(cls._COLUMNS + 6))
        scale_y = max(1.0, height / float(cls._ROWS + 5))

        def canvas(point):
            return (
                center_x + (point[0] - (cls._COLUMNS - 1) * 0.5) * scale_x,
                center_y + (point[1] - (cls._ROWS - 1) * 0.5) * scale_y,
                point[2],
            )

        points = tuple(canvas(point) for point in projected)
        occupied = {}
        shadow = set()
        for edge_index, (start_index, end_index) in enumerate(cls._EDGES):
            start = points[start_index]
            end = points[end_index]
            for sample_x, sample_y, sample_z in cls._edge_samples(
                    start, end, 8):
                key = (int(round(sample_x)), int(round(sample_y)))
                current = occupied.get(key)
                if current is None or sample_z >= current[0]:
                    occupied[key] = (sample_z, cls._edge_color(
                        palette, sample_z, edge_index))
        for point_x, point_y, point_z in points:
            key = (int(round(point_x)), int(round(point_y)))
            occupied[key] = (point_z + 0.02, palette[2])
        for point_x, point_y in occupied:
            shadow.add((point_x + 2, point_y + 1))
        commands = []
        for point_x, point_y in sorted(shadow):
            if (point_x, point_y) not in occupied:
                commands.append(renderer.fill(
                    point_x, point_y, 1, 1, ThemeColor.MUTED))
        for (point_x, point_y), (_depth, color) in sorted(
                occupied.items(), key=lambda item: item[1][0]):
            commands.append(renderer.fill(point_x, point_y, 2, 2, color))
        return commands

    def draw(self, renderer, state, bounds):
        angle_x = float(resolve(self.angle_x, state))
        angle_y = float(resolve(self.angle_y, state))
        angle_z = float(resolve(self.angle_z, state))
        phase = int(resolve(self.palette_phase, state)) % len(self._PALETTES)
        mode = str(resolve(self.mode, state)).lower()
        projected = self._projected_vertices(angle_x, angle_y, angle_z)
        palette = self._PALETTES[phase]
        x, y, width, height = bounds
        commands = [
            renderer.fill(x, y, width, height, ThemeColor.BACKGROUND),
            renderer.stroke(x, y, width, height, ThemeColor.BORDER, 1),
        ]
        content_bounds = self._content_bounds(bounds)
        if mode == "lines":
            commands += self._draw_line_mode(
                renderer, content_bounds, projected, palette)
        elif mode == "dots":
            commands += self._draw_dots_mode(
                renderer, content_bounds, projected, palette)
        else:
            commands += self._draw_text_mode(
                renderer, content_bounds, projected, palette)
        return commands


class BenchmarkStats(Component):
    """Compact fixed-cost statistics panel updated below animation rate."""

    covers_bounds = True
    _LABELS = (
        "COMMIT FPS\n"
        "F.MED\n"
        "F.P95\n"
        "TYPER\n"
        "FLUSH\n"
        "PYTHON\n"
        "MISSED"
    )

    def __init__(self, values, mode, status, key=None):
        super().__init__(key=key)
        self.values = values
        self.mode = mode
        self.status = status

    def draw(self, renderer, state, bounds):
        values = str(resolve(self.values, state))
        mode = str(resolve(self.mode, state)).upper()
        status = str(resolve(self.status, state))
        flagged = ("WARMUP" in status or "TIMEOUT" in status
                   or "FAILED" in status or "QUEUE" in status)
        status_color = (ThemeColor.WARNING if flagged
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
            x + width // 2, y + height - 46, mode, ThemeColor.SECONDARY,
            "JetBrainsMono Bold 8pt", "center", "middle",
            max_width=width - 24, truncate=True))
        commands.append(renderer.text(
            x + width // 2, y + height - 21, status, status_color,
            "JetBrainsMono Bold 8pt", "center", "middle",
            max_width=width - 24, truncate=True))
        return commands
