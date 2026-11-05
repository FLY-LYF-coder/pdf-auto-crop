"""The layers of the sheet and the boolean operations on the elements.

Two things the compositor learned to do: put anything anywhere in the
drawing order, and make one element out of several with a boolean
operation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rect(x0: float, y0: float, x1: float, y1: float, **kwargs
          ) -> comp.Shape:
    return comp.Shape(comp.SHAPE_RECT, fitz.Point(x0, y0),
                      fitz.Point(x1, y1), **kwargs)


def _ink(path: Path) -> int:
    """How many pixels of the page carry anything."""
    doc = fitz.open(str(path))
    try:
        page = doc[0]
        pix = page.get_pixmap(dpi=72)
        samples = pix.samples
        return sum(1 for i in range(0, len(samples), 3)
                   if samples[i] < 250 or samples[i + 1] < 250
                   or samples[i + 2] < 250)
    finally:
        doc.close()


def _figure(name: str = "f") -> comp.Figure:
    return comp.Figure(path=Path("probe.pdf"), page_index=0,
                       region=fitz.Rect(0, 0, 10, 10), name=name)


def _composition(*shapes: comp.Shape) -> comp.Composition:
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.shapes.extend(shapes)
    return sheet


# ---------------------------------------------------------------------------
# The layers
# ---------------------------------------------------------------------------


def test_a_sheet_from_before_the_layers_keeps_its_order() -> None:
    """A file without layers comes out as it was: figures, then elements."""
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.placements.append(comp.Placement(figure=_figure(),
                                            rect=fitz.Rect(0, 0, 10, 10)))
    sheet.shapes.append(_rect(20, 20, 40, 40))
    sheet.tables.append(comp.Table(rect=fitz.Rect(50, 50, 80, 70)))
    sheet.texts.append(comp.TextItem(text="note",
                                     position=fitz.Point(90, 90)))
    assert sheet.stack_order() == [
        ("figure", 0), ("shape", 0), ("table", 0), ("text", 0)]


def test_a_label_stands_just_above_its_figure() -> None:
    """The label travels with the figure it belongs to."""
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.placements.append(comp.Placement(figure=_figure(),
                                            rect=fitz.Rect(0, 0, 10, 10),
                                            label="图 1"))
    sheet.shapes.append(_rect(20, 20, 40, 40))
    order = sheet.draw_order()
    assert order.index(("figure", 0)) < order.index(("label", 0))
    # The label is above the figure, but the shape is still on the layer it
    # was put on.
    assert order.index(("label", 0)) > order.index(("shape", 0))


def test_a_new_object_goes_on_top() -> None:
    """What is added last is seen last."""
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.shapes.append(_rect(0, 0, 10, 10, z=sheet.next_z()))
    sheet.shapes.append(_rect(20, 20, 30, 30, z=sheet.next_z()))
    assert sheet.stack_order() == [("shape", 0), ("shape", 1)]
    assert sheet.shapes[1].z > sheet.shapes[0].z


def test_going_to_the_top_and_to_the_bottom() -> None:
    """The two buttons that were there before now do what they say."""
    sheet = _composition(_rect(0, 0, 10, 10), _rect(20, 20, 30, 30),
                         _rect(40, 40, 50, 50))
    assert sheet.restack([("shape", 0)], "top")
    assert sheet.stack_order() == [("shape", 1), ("shape", 2), ("shape", 0)]
    assert sheet.restack([("shape", 2)], "bottom")
    assert sheet.stack_order() == [("shape", 2), ("shape", 1), ("shape", 0)]


def test_going_up_and_down_swaps_with_the_next_one() -> None:
    """One layer up means one element up."""
    sheet = _composition(_rect(0, 0, 10, 10), _rect(20, 20, 30, 30),
                         _rect(40, 40, 50, 50))
    assert sheet.restack([("shape", 1)], "up")
    assert sheet.stack_order() == [("shape", 0), ("shape", 2), ("shape", 1)]
    assert sheet.restack([("shape", 2)], "down")
    assert sheet.stack_order() == [("shape", 2), ("shape", 0), ("shape", 1)]


def test_a_selection_moves_as_one_block() -> None:
    """Several things picked move together, the way a layer does."""
    sheet = _composition(_rect(0, 0, 10, 10), _rect(20, 20, 30, 30),
                         _rect(40, 40, 50, 50))
    assert sheet.restack([("shape", 0), ("shape", 1)], "up")
    # The block has moved above the third shape.
    assert sheet.stack_order() == [("shape", 2), ("shape", 0), ("shape", 1)]
    assert sheet.restack([("shape", 0), ("shape", 1)], "down")
    assert sheet.stack_order() == [("shape", 0), ("shape", 1), ("shape", 2)]
    assert sheet.restack([("shape", 0), ("shape", 1)], "top")
    assert sheet.stack_order() == [("shape", 2), ("shape", 0), ("shape", 1)]
    # The block is on top: there is no room above it.
    assert not sheet.restack([("shape", 0), ("shape", 1)], "up")


def test_the_layers_reach_across_the_kinds() -> None:
    """A figure, a table and a text share one order."""
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.placements.append(comp.Placement(figure=_figure(),
                                            rect=fitz.Rect(0, 0, 10, 10)))
    sheet.tables.append(comp.Table(rect=fitz.Rect(50, 50, 80, 70)))
    sheet.texts.append(comp.TextItem(text="note",
                                     position=fitz.Point(90, 90)))
    assert sheet.restack([("table", 0)], "top")
    assert sheet.stack_order()[-1] == ("table", 0)
    assert sheet.restack([("table", 0)], "bottom")
    assert sheet.stack_order()[0] == ("table", 0)


def test_nothing_moves_when_there_is_no_room() -> None:
    """The top element cannot go any higher."""
    sheet = _composition(_rect(0, 0, 10, 10))
    assert not sheet.restack([("shape", 0)], "up")
    assert not sheet.restack([("shape", 0)], "down")
    assert not sheet.restack([("shape", 0)], "top")


def test_the_layers_survive_a_save_and_a_load(tmp_path) -> None:
    """The file keeps the order."""
    sheet = _composition(_rect(0, 0, 10, 10), _rect(20, 20, 30, 30))
    sheet.restack([("shape", 0)], "top")
    path = tmp_path / "sheet.json"
    comp.save_composition(sheet, path)
    back = comp.load_composition(path)
    assert back.stack_order() == sheet.stack_order()
    assert back.shapes[0].z == pytest.approx(sheet.shapes[0].z)


# ---------------------------------------------------------------------------
# The boolean operations
# ---------------------------------------------------------------------------


def _two_squares() -> tuple:
    a = _rect(20, 20, 80, 80, fill=True, fill_color=(1.0, 0.0, 0.0),
              fill_opacity=1.0, color=(0.0, 0.0, 0.0), width=0.0)
    b = _rect(50, 50, 110, 110, fill=True, fill_color=(0.0, 0.0, 1.0),
              fill_opacity=1.0, color=(0.0, 0.0, 0.0), width=0.0)
    return a, b


@pytest.mark.parametrize("op,expected", [
    (comp.BOOLEAN_UNION, 6300),
    (comp.BOOLEAN_INTERSECT, 900),
    (comp.BOOLEAN_DIFFERENCE, 2700),
    (comp.BOOLEAN_EXCLUDE, 5400),
])
def test_the_operations_leave_the_right_area(tmp_path, op, expected) -> None:
    """The area the operation leaves is the area it should leave."""
    a, b = _two_squares()
    sheet = _composition()
    sheet.shapes.append(comp.boolean_result(op, [a, b]))
    out = tmp_path / f"{op}.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(200.0, 200.0))
    assert _ink(out) == pytest.approx(expected, abs=expected * 0.01)


def test_the_result_is_still_vector(tmp_path) -> None:
    """A boolean result is not a picture."""
    a, b = _two_squares()
    sheet = _composition()
    sheet.shapes.append(comp.boolean_result(comp.BOOLEAN_UNION, [a, b]))
    out = tmp_path / "union.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(200.0, 200.0))
    doc = fitz.open(str(out))
    try:
        assert len(doc[0].get_images()) == 0
    finally:
        doc.close()


def test_the_result_keeps_the_look_of_the_base() -> None:
    """The base decides how the result looks."""
    a, b = _two_squares()
    a.color = (0.1, 0.2, 0.3)
    a.width = 2.5
    a.text = "base"
    result = comp.boolean_result(comp.BOOLEAN_DIFFERENCE, [a, b])
    assert result.kind == comp.SHAPE_BOOLEAN
    assert result.op == comp.BOOLEAN_DIFFERENCE
    assert result.color == pytest.approx((0.1, 0.2, 0.3))
    assert result.width == pytest.approx(2.5)
    assert result.text == "base"
    assert len(result.parts) == 2


def test_the_result_takes_the_place_of_the_shapes() -> None:
    """The box of the result is the box of the shapes it was made from."""
    a, b = _two_squares()
    result = comp.boolean_result(comp.BOOLEAN_UNION, [a, b])
    assert result.rect.x0 == pytest.approx(20.0)
    assert result.rect.y0 == pytest.approx(20.0)
    assert result.rect.x1 == pytest.approx(110.0)
    assert result.rect.y1 == pytest.approx(110.0)


def test_the_result_travels(tmp_path) -> None:
    """A result that is moved takes its shapes with it."""
    a, b = _two_squares()
    result = comp.boolean_result(comp.BOOLEAN_UNION, [a, b])
    moved = comp.transform_boolean(result, fitz.Rect(40, 40, 130, 130), False)
    assert moved.parts[0].start.x == pytest.approx(40.0)
    assert moved.parts[0].start.y == pytest.approx(40.0)
    assert moved.rect.x0 == pytest.approx(40.0)


def test_the_result_can_be_made_bigger() -> None:
    """A result that is made bigger takes its shapes with it."""
    a, b = _two_squares()
    result = comp.boolean_result(comp.BOOLEAN_UNION, [a, b])
    bigger = comp.transform_boolean(result, fitz.Rect(0, 0, 220, 220), True)
    assert bigger.rect.width == pytest.approx(220.0)
    # The two squares keep their places inside the bigger box.
    assert bigger.parts[0].rect.width == pytest.approx(60.0 * 220.0 / 90.0)
    assert bigger.parts[0].rect.x0 == pytest.approx(0.0)


def test_the_result_survives_a_save_and_a_load(tmp_path) -> None:
    """The file keeps the operation and the shapes behind it."""
    a, b = _two_squares()
    sheet = _composition()
    sheet.shapes.append(comp.boolean_result(comp.BOOLEAN_EXCLUDE, [a, b]))
    path = tmp_path / "sheet.json"
    comp.save_composition(sheet, path)
    back = comp.load_composition(path)
    shape = back.shapes[0]
    assert shape.op == comp.BOOLEAN_EXCLUDE
    assert len(shape.parts) == 2
    assert shape.parts[0].kind == comp.SHAPE_RECT


def test_a_line_cannot_take_part() -> None:
    """A line has no inside to keep or to take away."""
    assert comp.can_boolean(_rect(0, 0, 10, 10))
    assert not comp.can_boolean(comp.Shape(comp.SHAPE_LINE,
                                           fitz.Point(0, 0),
                                           fitz.Point(10, 10)))
    assert not comp.can_boolean(comp.Shape(comp.SHAPE_BRACE_LEFT,
                                           fitz.Point(0, 0),
                                           fitz.Point(10, 10)))


def test_a_result_cannot_take_part() -> None:
    """A result is an area, not a path: there is no way to clip by it."""
    a, b = _two_squares()
    result = comp.boolean_result(comp.BOOLEAN_UNION, [a, b])
    assert not comp.can_boolean(result)
    with pytest.raises(ValueError):
        comp.boolean_result(comp.BOOLEAN_UNION, [a, result])


def test_an_unknown_operation_is_refused() -> None:
    """The operation has to be one of the four."""
    a, b = _two_squares()
    with pytest.raises(ValueError):
        comp.boolean_result("sideways", [a, b])
    with pytest.raises(ValueError):
        comp.boolean_result(comp.BOOLEAN_UNION, [a])


def test_a_result_can_be_turned(tmp_path) -> None:
    """The whole compound turns about its own middle."""
    a, b = _two_squares()
    result = comp.boolean_result(comp.BOOLEAN_UNION, [a, b])
    result.rotation = 90.0
    sheet = _composition(result)
    out = tmp_path / "turned.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(300.0, 300.0))
    doc = fitz.open(str(out))
    try:
        assert len(doc[0].get_images()) == 0
        drawings = doc[0].get_drawings()
        assert drawings, "the result was not drawn at all"
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# The layers in the file
# ---------------------------------------------------------------------------


def _source(path: Path) -> tuple[Path, fitz.Rect]:
    """A page with a red square, and the region it stands in."""
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.draw_rect(fitz.Rect(20, 20, 120, 120), color=(1, 0, 0),
                   fill=(1, 0, 0), width=0)
    doc.save(str(path))
    doc.close()
    return path, fitz.Rect(20, 80, 120, 180)


def _colour_at(path: Path, x: float, y: float) -> tuple[int, int, int]:
    """The colour of a point on the page, in user space."""
    doc = fitz.open(str(path))
    try:
        pix = doc[0].get_pixmap(dpi=72)
        row = int(pix.height - y)
        col = int(x)
        i = (row * pix.width + col) * 3
        return tuple(int(v) for v in pix.samples[i:i + 3])
    finally:
        doc.close()


def test_the_layer_order_is_the_order_in_the_file(tmp_path) -> None:
    """What is on top on the canvas is on top in the file."""
    src, region = _source(tmp_path / "src.pdf")
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(20, 20, 120, 120)))
    sheet.shapes.append(_rect(20, 20, 120, 120, fill=True,
                               fill_color=(0.0, 0.0, 1.0),
                               fill_opacity=1.0, color=(0.0, 0.0, 1.0),
                               width=0.0))
    out = tmp_path / "top.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(200.0, 200.0))
    blue = _colour_at(out, 70, 70)
    assert blue[2] > 200 and blue[0] < 60, str(blue)

    # The shape goes to the bottom: the figure shows through again.
    sheet.restack([("shape", 0)], "bottom")
    out = tmp_path / "bottom.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(200.0, 200.0))
    red = _colour_at(out, 70, 70)
    assert red[0] > 200 and red[2] < 60, str(red)
