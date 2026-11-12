"""Making several elements the same size.

「一样大」 originally took two elements: the first one was changed to the
size of the second.  Several elements can be picked, so several of them
ought to be able to be made the same size at once -- the last one picked is
the reference, and every other one is changed to match it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402


# ---------------------------------------------------------------------------
# The model: the box an element takes when it is made the size of another
# ---------------------------------------------------------------------------


def test_making_it_the_same_width_keeps_its_own_proportions() -> None:
    """同宽: the width of the reference, the proportions of the element."""
    target = fitz.Rect(0, 0, 100, 50)
    source = fitz.Rect(0, 0, 200, 200)
    new = comp.match_size_rect(target, source, "width")
    assert new.width == pytest.approx(200.0)
    assert new.height == pytest.approx(100.0)


def test_making_it_the_same_height_keeps_its_own_proportions() -> None:
    """同高: the height of the reference, the proportions of the element."""
    target = fitz.Rect(0, 0, 100, 50)
    source = fitz.Rect(0, 0, 200, 200)
    new = comp.match_size_rect(target, source, "height")
    assert new.height == pytest.approx(200.0)
    assert new.width == pytest.approx(400.0)


def test_making_it_the_same_size_takes_the_size_as_it_is() -> None:
    """等大: exactly the size of the reference, proportions be damned."""
    target = fitz.Rect(0, 0, 100, 50)
    source = fitz.Rect(0, 0, 200, 80)
    new = comp.match_size_rect(target, source, "both")
    assert new.width == pytest.approx(200.0)
    assert new.height == pytest.approx(80.0)


def test_the_element_keeps_its_own_corner() -> None:
    """The element is made bigger where it stands, not moved somewhere else."""
    target = fitz.Rect(30, 40, 130, 90)
    source = fitz.Rect(0, 0, 200, 200)
    new = comp.match_size_rect(target, source, "both")
    assert (new.x0, new.y0) == pytest.approx((30.0, 40.0))


def test_a_degenerate_element_does_not_crash_the_size() -> None:
    """An element with no width cannot be scaled by its width."""
    target = fitz.Rect(0, 0, 0, 50)
    source = fitz.Rect(0, 0, 200, 200)
    new = comp.match_size_rect(target, source, "width")
    assert new.width == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# The panel: several elements at once
# ---------------------------------------------------------------------------


def _panel():
    """A panel with three figures of different sizes on the sheet."""
    import tkinter as tk

    from pdf_auto_crop import compositor_gui

    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover -- headless box
        pytest.skip("no display")
    panel = compositor_gui.ComposePanel(root)
    figure = comp.Figure(path=Path("probe.pdf"), page_index=0,
                         region=fitz.Rect(0, 0, 10, 10))
    panel.composition.placements.extend([
        comp.Placement(figure=figure, rect=fitz.Rect(0, 0, 100, 50)),
        comp.Placement(figure=figure, rect=fitz.Rect(0, 60, 40, 140)),
        comp.Placement(figure=figure, rect=fitz.Rect(0, 150, 200, 250)),
    ])
    return root, panel


def test_three_elements_are_made_the_same_size() -> None:
    """Three figures picked: all three end up with the last one's width."""
    root, panel = _panel()
    try:
        panel._sel_figs = [0, 1, 2]
        panel._last_picked = ("figure", 2)
        panel._match_size("width")
        widths = [p.rect.width for p in panel.composition.placements]
        assert widths == pytest.approx([200.0, 200.0, 200.0])
        # The reference itself has not moved.
        assert panel.composition.placements[2].rect.x0 == pytest.approx(0.0)
        assert panel.composition.placements[2].rect.y0 == pytest.approx(150.0)
    finally:
        panel.destroy()
        root.destroy()


def test_the_same_size_keeps_the_proportions_of_each_element() -> None:
    """Each element keeps its own shape: only the width is copied."""
    root, panel = _panel()
    try:
        panel._sel_figs = [0, 1, 2]
        panel._last_picked = ("figure", 2)
        panel._match_size("width")
        first = panel.composition.placements[0].rect
        second = panel.composition.placements[1].rect
        assert first.width == pytest.approx(200.0)
        assert first.height == pytest.approx(100.0)
        assert second.width == pytest.approx(200.0)
        assert second.height == pytest.approx(400.0)
    finally:
        panel.destroy()
        root.destroy()


def test_the_same_height_works_on_several_elements_too() -> None:
    """同高 with three elements: every one gets the reference's height."""
    root, panel = _panel()
    try:
        panel._sel_figs = [0, 1, 2]
        panel._last_picked = ("figure", 2)
        panel._match_size("height")
        heights = [p.rect.height for p in panel.composition.placements]
        assert heights == pytest.approx([100.0, 100.0, 100.0])
    finally:
        panel.destroy()
        root.destroy()


def test_the_last_picked_element_is_the_reference() -> None:
    """The element picked last is the one the others are measured against."""
    root, panel = _panel()
    try:
        panel._sel_figs = [0, 1, 2]
        panel._last_picked = ("figure", 1)
        panel._match_size("both")
        assert panel.composition.placements[2].rect.width == pytest.approx(40.0)
        assert panel.composition.placements[0].rect.width == pytest.approx(40.0)
    finally:
        panel.destroy()
        root.destroy()


def test_one_element_alone_is_not_enough() -> None:
    """A single element has nothing to be measured against."""
    root, panel = _panel()
    try:
        panel._sel_figs = [0]
        panel._last_picked = ("figure", 0)
        panel._match_size("width")
        assert panel.composition.placements[0].rect.width == pytest.approx(100.0)
    finally:
        panel.destroy()
        root.destroy()


# ---------------------------------------------------------------------------
# Aligning: it already took several elements
# ---------------------------------------------------------------------------


def test_aligning_several_elements_lines_them_up_with_each_other() -> None:
    """对齐 with three elements: every left edge lands on the leftmost one."""
    rects = [fitz.Rect(30, 0, 60, 20),
             fitz.Rect(10, 30, 40, 50),
             fitz.Rect(50, 60, 80, 80)]
    target = comp.align_target(rects, comp.ALIGN_LEFT)
    assert target == pytest.approx(10.0)
    for r in rects:
        moved = comp.aligned_rect(r, comp.ALIGN_LEFT, target)
        assert moved.x0 == pytest.approx(10.0)
        # The size is kept: aligning moves, it does not resize.
        assert moved.width == pytest.approx(r.width)
