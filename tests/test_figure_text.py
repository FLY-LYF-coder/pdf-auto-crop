"""Tests for folding a figure's own labels into the box that was detected.

The detector works on the raster and finds the *ink* of a plot.  The axis
ticks, the axis titles and the figure's title are separate ink components
just outside the frame, so they are not part of the detected box -- and a
crop taken from that box loses them.

These tests cover the folding itself and, at the end, the whole way round:
a synthetic plot, detected and cropped, has to come out with its axes.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
import pytest

from pdf_auto_crop import compositor as comp
from pdf_auto_crop import figure_text as ft


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Fig. 3: the results",
    "Figure 2 shows ...",
    "FIGURE 7",
    "图 4  实验结果",
    "表 2  数据",
    "Table 1: inputs",
])
def test_a_caption_is_told_apart(text: str) -> None:
    assert ft.is_caption(text)


@pytest.mark.parametrize("text", [
    "the amplitude of the response",
    "0.5",
    "time t (s)",
    "a curve",
    "sin cos",
    "Figure this out",
])
def test_an_ordinary_text_is_not_a_caption(text: str) -> None:
    assert not ft.is_caption(text)


# ---------------------------------------------------------------------------
# Folding
# ---------------------------------------------------------------------------


def _block(x0, y0, x1, y1, text="label"):
    return (fitz.Rect(x0, y0, x1, y1), text)


def test_a_label_next_to_the_region_is_folded_in() -> None:
    region = fitz.Rect(100, 100, 300, 300)
    blocks = [_block(105, 310, 200, 320, "time t (s)")]
    grown = ft.expand_region_with_text(region, blocks)
    assert grown.y1 == 320
    assert grown.x0 == 100


def test_a_label_far_away_is_left_out() -> None:
    region = fitz.Rect(100, 100, 300, 300)
    blocks = [_block(105, 500, 200, 510, "a footnote")]
    grown = ft.expand_region_with_text(region, blocks)
    assert grown == region


def test_a_caption_is_left_out() -> None:
    """A caption belongs to the figure, but it is not part of it."""
    region = fitz.Rect(100, 100, 300, 300)
    blocks = [_block(100, 310, 400, 330, "Fig. 1: the whole story")]
    grown = ft.expand_region_with_text(region, blocks)
    assert grown == region


def test_body_text_is_left_out() -> None:
    """Body text is wide; an axis label is not."""
    region = fitz.Rect(100, 100, 300, 300)
    blocks = [_block(60, 310, 450, 340, "a paragraph of running text")]
    grown = ft.expand_region_with_text(region, blocks)
    assert grown == region


def test_text_inside_another_figure_is_left_out() -> None:
    region = fitz.Rect(100, 100, 300, 300)
    other = fitz.Rect(320, 100, 500, 300)
    blocks = [_block(320, 110, 400, 120, "the other figure")]
    grown = ft.expand_region_with_text(region, blocks, others=[other])
    assert grown == region


def test_the_growth_repeats_until_it_settles() -> None:
    """A label pulled in by another label is followed in."""
    region = fitz.Rect(100, 100, 300, 300)
    blocks = [
        _block(100, 310, 200, 320, "first"),
        _block(100, 330, 200, 340, "second"),
    ]
    grown = ft.expand_region_with_text(region, blocks)
    assert grown.y1 == 340


def test_the_margin_is_the_larger_of_the_two_neighbours() -> None:
    """Every region of a page is measured against its own size."""
    small = fitz.Rect(100, 100, 140, 140)
    blocks = [_block(140, 145, 160, 150, "close by")]
    grown = ft.expand_region_with_text(small, blocks)
    assert grown.x1 == 160


def test_expanding_several_regions_keeps_them_apart() -> None:
    regions = [fitz.Rect(100, 100, 300, 300), fitz.Rect(320, 100, 500, 300)]
    blocks = [_block(100, 310, 200, 320, "under the first")]
    grown = ft.expand_regions_with_text(regions, blocks)
    assert grown[0].y1 == 320
    assert grown[1] == regions[1]


def test_the_text_blocks_of_a_page_are_read() -> None:
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.insert_text(fitz.Point(20, 100), "hello", fontsize=10)
    blocks = ft.text_blocks(page)
    doc.close()
    assert blocks
    assert any("hello" in text for _, text in blocks)


# ---------------------------------------------------------------------------
# The whole way round
# ---------------------------------------------------------------------------


def _plot_pdf(path: Path) -> None:
    """A plot: a frame, a curve inside it, and labels all around it."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=240)
    page.draw_rect(fitz.Rect(80, 40, 260, 180), color=(0, 0, 0), width=1)
    page.draw_line(fitz.Point(90, 160), fitz.Point(250, 60),
                   color=(0, 0, 1), width=2)
    # The labels a detector cannot see: they are outside the frame.
    page.insert_text(fitz.Point(120, 195), "time t (s)", fontsize=9)
    page.insert_text(fitz.Point(20, 100), "amplitude", fontsize=9)
    page.insert_text(fitz.Point(150, 25), "a curve", fontsize=9)
    doc.save(str(path))
    doc.close()


def test_a_detected_plot_keeps_its_axes(tmp_path) -> None:
    """The end of the story: the crop has to carry the labels with it.

    Without the folding, the detected box is the frame alone and the crop
    comes out without a single axis label.
    """
    src = tmp_path / "plot.pdf"
    _plot_pdf(src)

    found = comp.detect_figures(src)
    assert len(found) == 1
    region = found[0].region
    # The frame is at x 80..260, y 40..180 in page space; the labels reach
    # x 20..260 and y 25..195.
    assert region.x0 < 60, region        # the y-axis title
    assert region.y1 > 200, region        # the x-axis title
    assert region.y0 < 45, region         # the figure's own title


def test_a_caption_stays_out_of_the_crop(tmp_path) -> None:
    """A caption below the figure must not come along."""
    src = tmp_path / "plot_with_caption.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=260)
    page.draw_rect(fitz.Rect(80, 40, 260, 180), color=(0, 0, 0), width=1)
    page.draw_line(fitz.Point(90, 160), fitz.Point(250, 60),
                   color=(0, 0, 1), width=2)
    page.insert_text(fitz.Point(90, 200), "Fig. 1: the response of the system",
                     fontsize=9)
    doc.save(str(src))
    doc.close()

    found = comp.detect_figures(src)
    assert len(found) == 1
    region = found[0].region
    # The caption sits below the frame; the sheet's y points up, so it is
    # at a *smaller* y than the frame's bottom edge.
    caption = fitz.Rect(90, 260 - 209, 280, 260 - 200)
    assert not region.intersects(caption), region
