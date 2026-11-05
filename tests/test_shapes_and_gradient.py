"""The shape catalogue, the gradient fill and the formula.

Three things the compositor learned to do: draw the shapes PowerPoint
offers, fill a graphic element with a gradient, and recognise a text that
is written like a formula.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402
from pdf_auto_crop.tex import looks_like_math  # noqa: E402


# ---------------------------------------------------------------------------
# The formula
# ---------------------------------------------------------------------------


def test_a_text_written_like_a_formula_is_one() -> None:
    """``$...$`` and ``\\[...\\]`` are how a formula is written down."""
    assert looks_like_math("$x^2$")
    assert looks_like_math("  $E = mc^2$  ")
    assert looks_like_math("\\[x^2\\]")
    assert not looks_like_math("x^2")
    assert not looks_like_math("a $ b")
    assert not looks_like_math("")


# ---------------------------------------------------------------------------
# The shape catalogue
# ---------------------------------------------------------------------------


def test_every_shape_has_an_outline_or_is_drawn_by_hand() -> None:
    """No shape may be left empty."""
    drawn_by_hand = (comp.SHAPE_ARROW, comp.SHAPE_LINE, comp.SHAPE_RECT,
                     comp.SHAPE_ROUNDRECT, comp.SHAPE_ELLIPSE)
    box = fitz.Rect(0.0, 0.0, 100.0, 60.0)
    for kind in comp.SHAPE_KINDS:
        points = comp.shape_outline(kind, box)
        if kind in drawn_by_hand:
            assert not points, kind
            continue
        assert len(points) >= 3, kind


def test_the_outlines_stay_inside_their_box() -> None:
    """A shape is drawn in the box it spans, like a PowerPoint shape."""
    box = fitz.Rect(10.0, 20.0, 110.0, 80.0)
    for kind in comp.SHAPE_KINDS:
        for point in comp.shape_outline(kind, box):
            assert box.x0 - 0.01 <= point.x <= box.x1 + 0.01, kind
            assert box.y0 - 0.01 <= point.y <= box.y1 + 0.01, kind


def test_the_left_arrow_is_the_right_arrow_mirrored() -> None:
    box = fitz.Rect(0.0, 0.0, 100.0, 50.0)
    right = comp.shape_outline(comp.SHAPE_RIGHT_ARROW, box)
    left = comp.shape_outline(comp.SHAPE_LEFT_ARROW, box)
    assert len(right) == len(left)
    for a, b in zip(right, left):
        assert a.x == pytest.approx(box.x1 - b.x)
        assert a.y == pytest.approx(b.y)


def test_the_star_has_its_points() -> None:
    """A five-pointed star has ten corners."""
    points = comp.shape_outline(comp.SHAPE_STAR, fitz.Rect(0, 0, 100, 100))
    assert len(points) == 10
    # The tips reach further out than the hollows between them.
    radii = [math.hypot(p.x - 50, p.y - 50) for p in points]
    assert max(radii) == pytest.approx(50.0, abs=0.01)
    assert min(radii) < 30.0


def test_the_catalogue_grew() -> None:
    """The shapes on offer are the ones PowerPoint offers, and more."""
    assert len(comp.SHAPE_KINDS) >= 40, len(comp.SHAPE_KINDS)
    for kind in (comp.SHAPE_STAR, comp.SHAPE_DOCUMENT, comp.SHAPE_MATH_PLUS,
                 comp.SHAPE_CROSS_ARROW, comp.SHAPE_CLOUD,
                 comp.SHAPE_DOUBLE_ARROW, comp.SHAPE_HEXAGON):
        assert kind in comp.SHAPE_KINDS


# ---------------------------------------------------------------------------
# The gradient fill
# ---------------------------------------------------------------------------


def _gradient_shape(**kwargs) -> comp.Shape:
    data = dict(
        kind=comp.SHAPE_RECT,
        start=fitz.Point(20.0, 20.0),
        end=fitz.Point(140.0, 90.0),
        color=(0.0, 0.0, 0.0),
        width=1.0,
        fill=True,
        fill_color=(1.0, 0.0, 0.0),
        fill_opacity=1.0,
        fill_gradient=(0.0, 0.0, 1.0),
        gradient_angle=0.0,
    )
    data.update(kwargs)
    return comp.Shape(**data)


def test_the_gradient_is_painted_into_the_shape(tmp_path) -> None:
    """The fill is a gradient, and it stays vector."""
    doc = fitz.open()
    page = doc.new_page(width=200, height=120)
    comp.draw_shape(page, _gradient_shape(), 120)
    out = tmp_path / "gradient.pdf"
    doc.save(str(out))
    doc.close()

    doc = fitz.open(str(out))
    try:
        page = doc[0]
        assert len(page.get_images()) == 0, "the gradient became a picture"
        pix = page.get_pixmap(dpi=72)
        samples = pix.samples
        width = pix.width
        row = pix.height // 2
        reds = [samples[(row * width + x) * 3] for x in range(25, 135, 10)]
        blues = [samples[(row * width + x) * 3 + 2] for x in range(25, 135, 10)]
        # The red goes down and the blue goes up: that is the gradient.
        assert reds[0] > reds[-1], reds
        assert blues[0] < blues[-1], blues
    finally:
        doc.close()


def test_a_plain_fill_is_not_a_gradient(tmp_path) -> None:
    """Without a second colour the fill is a plain one."""
    shape = _gradient_shape(fill_gradient=None)
    assert shape.fill_gradient is None
    doc = fitz.open()
    page = doc.new_page(width=200, height=120)
    comp.draw_shape(page, shape, 120)
    out = tmp_path / "plain.pdf"
    doc.save(str(out))
    doc.close()
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=72)
        samples = pix.samples
        width = pix.width
        row = pix.height // 2
        reds = [samples[(row * width + x) * 3] for x in range(25, 135, 10)]
        assert len(set(reds)) == 1, reds
    finally:
        doc.close()


def test_the_gradient_survives_a_save_and_a_load() -> None:
    """The layout file keeps the gradient."""
    shape = _gradient_shape()
    back = comp.Shape.from_dict(shape.to_dict())
    assert back.fill_gradient == pytest.approx((0.0, 0.0, 1.0))
    assert back.gradient_angle == pytest.approx(0.0)
    assert back.fill_color == pytest.approx((1.0, 0.0, 0.0))


def test_the_gradient_is_part_of_the_look_of_a_shape() -> None:
    """The format painter carries the gradient along."""
    shape = _gradient_shape()
    fmt = comp.format_of(shape, "shape")
    assert "fill_gradient" in fmt
    assert "gradient_angle" in fmt
    painted = comp.with_format(_gradient_shape(fill_gradient=(0.0, 1.0, 0.0)),
                               fmt)
    assert painted.fill_gradient == pytest.approx((0.0, 0.0, 1.0))
