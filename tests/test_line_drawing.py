"""Drawing a straight line of a known length.

Two things were hard: a line that is exactly horizontal or exactly vertical
cannot be drawn by hand, and a line of exactly 20 mm cannot be drawn by
hand either.  The shift key holds a line straight, and the panel takes the
length in millimetres.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tkinter as tk  # noqa: E402
import pymupdf as fitz  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _panel():
    """A panel with a sheet to draw on."""
    from pdf_auto_crop import compositor_gui

    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover -- headless box
        pytest.skip("no display")
    panel = compositor_gui.ComposePanel(root)
    panel.pack(fill=tk.BOTH, expand=True)
    root.update()
    return root, panel


def _drag(panel, x0, y0, x1, y1, state: int = 0) -> None:
    """Press, move and release, the way a drag is made."""
    canvas = panel.canvas
    canvas.event_generate("<Button-1>", x=x0, y=y0, state=state)
    panel.update()
    for step in range(1, 4):
        canvas.event_generate("<B1-Motion>",
                              x=x0 + (x1 - x0) * step // 3,
                              y=y0 + (y1 - y0) * step // 3,
                              state=state)
        panel.update()
    canvas.event_generate("<ButtonRelease-1>", x=x1, y=y1, state=state)
    panel.update()


#: The shift key, as Tk counts it.
SHIFT = 0x0001


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def test_a_line_pulled_onto_the_horizontal_stays_there() -> None:
    """A drag that goes mostly sideways becomes a horizontal line."""
    start = fitz.Point(0, 0)
    end = comp.constrain_to_axis(start, fitz.Point(100, 8))
    assert end.y == pytest.approx(0.0)
    assert end.x == pytest.approx(100.0)


def test_a_line_pulled_onto_the_vertical_stays_there() -> None:
    """A drag that goes mostly up and down becomes a vertical line."""
    start = fitz.Point(0, 0)
    end = comp.constrain_to_axis(start, fitz.Point(8, 100))
    assert end.x == pytest.approx(0.0)
    assert end.y == pytest.approx(100.0)


def test_the_length_of_a_line_is_the_distance_of_its_two_ends() -> None:
    """A line is as long as the distance between its ends."""
    shape = comp.Shape(comp.SHAPE_LINE, fitz.Point(0, 0),
                       fitz.Point(30, 40))
    assert comp.shape_length(shape) == pytest.approx(50.0)


def test_the_length_of_a_box_is_its_longer_side() -> None:
    """A box is as long as its longer side."""
    shape = comp.Shape(comp.SHAPE_RECT, fitz.Point(0, 0),
                       fitz.Point(30, 80))
    assert comp.shape_length(shape) == pytest.approx(80.0)


def test_a_line_of_a_given_length_keeps_its_direction() -> None:
    """The line is made longer or shorter, but it still points the same way."""
    shape = comp.Shape(comp.SHAPE_LINE, fitz.Point(0, 0),
                       fitz.Point(30, 40))
    longer = comp.set_shape_length(shape, 100.0)
    assert comp.shape_length(longer) == pytest.approx(100.0)
    # The direction has not changed.
    assert longer.end.x / longer.end.y == pytest.approx(30 / 40)
    assert longer.start.x == pytest.approx(0.0)


def test_a_box_of_a_given_length_keeps_its_proportions() -> None:
    """The box is scaled, so it still looks the way it did."""
    shape = comp.Shape(comp.SHAPE_RECT, fitz.Point(0, 0),
                       fitz.Point(30, 60))
    longer = comp.set_shape_length(shape, 120.0)
    assert comp.shape_length(longer) == pytest.approx(120.0)
    assert longer.rect.width == pytest.approx(60.0)
    assert longer.rect.height == pytest.approx(120.0)


def test_a_box_of_a_given_length_stays_where_it_is() -> None:
    """The box grows about its own middle, not away from it."""
    shape = comp.Shape(comp.SHAPE_RECT, fitz.Point(0, 0),
                       fitz.Point(30, 60))
    longer = comp.set_shape_length(shape, 120.0)
    assert longer.rect.x0 == pytest.approx(-15.0)
    assert longer.rect.y0 == pytest.approx(-30.0)


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------


def test_a_line_drawn_with_the_shift_key_is_straight() -> None:
    """Holding the shift key makes the line exactly horizontal."""
    root, panel = _panel()
    try:
        panel._pick_tool(comp.SHAPE_LINE)
        x0, y0 = panel._to_canvas(50, 100)
        x1, y1 = panel._to_canvas(150, 112)
        _drag(panel, x0, y0, x1, y1, state=SHIFT)
        assert panel.composition.shapes, "nothing was drawn"
        shape = panel.composition.shapes[-1]
        assert shape.start.y == pytest.approx(shape.end.y, abs=0.6)
    finally:
        panel.destroy()
        root.destroy()


def test_a_line_drawn_without_the_shift_key_keeps_its_slope() -> None:
    """Without the shift key the line is what the cursor drew."""
    root, panel = _panel()
    try:
        panel._pick_tool(comp.SHAPE_LINE)
        x0, y0 = panel._to_canvas(50, 100)
        x1, y1 = panel._to_canvas(150, 112)
        _drag(panel, x0, y0, x1, y1)
        shape = panel.composition.shapes[-1]
        assert shape.start.y != pytest.approx(shape.end.y, abs=0.6)
    finally:
        panel.destroy()
        root.destroy()


def test_the_length_of_the_line_can_be_written_in_millimetres() -> None:
    """The panel takes the length of the line in millimetres."""
    root, panel = _panel()
    try:
        panel._pick_tool(comp.SHAPE_LINE)
        x0, y0 = panel._to_canvas(50, 100)
        x1, y1 = panel._to_canvas(150, 112)
        _drag(panel, x0, y0, x1, y1)
        shape = panel.composition.shapes[-1]
        assert shape is panel.composition.shapes[-1]
        panel._sel_shapes = [len(panel.composition.shapes) - 1]
        panel._selected_shape = len(panel.composition.shapes) - 1
        panel._sync_properties()
        # The panel shows the length of the line.
        assert panel.var_shape_length.get() != ""
        panel.var_shape_length.set("20")
        panel.update()
        new = panel.composition.shapes[-1]
        assert comp.shape_length(new) == pytest.approx(20 * 72 / 25.4,
                                                       rel=0.01)
    finally:
        panel.destroy()
        root.destroy()
