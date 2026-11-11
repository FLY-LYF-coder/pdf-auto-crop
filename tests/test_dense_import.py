"""The import of a folder of big vector PDFs.

A plot that was drawn with hundreds of thousands of points is a huge PDF,
and rendering it takes minutes.  Such a page is one figure, and it is taken
as it is; the rest is found the usual way.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402


def _two_panels(path: Path) -> None:
    """A page with two figures on it."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=200)
    page.draw_rect(fitz.Rect(20, 20, 180, 180), color=(0, 0, 0),
                   fill=(0, 0, 0), width=1)
    page.draw_rect(fitz.Rect(220, 20, 380, 180), color=(0, 0, 0),
                   fill=(0, 0, 0), width=1)
    doc.save(str(path))
    doc.close()


def test_a_dense_page_is_taken_whole(tmp_path, monkeypatch) -> None:
    """A page with a huge amount of drawing is one figure, the whole page."""
    src = tmp_path / "panels.pdf"
    _two_panels(src)
    # Every page counts as dense here.
    monkeypatch.setattr(comp, "DENSE_CONTENT_BYTES", 10)
    started = time.time()
    found = comp.detect_figures(src)
    assert len(found) == 1, found
    # The whole page is the figure: the margins are left in.
    assert found[0].region.width == pytest.approx(400.0, abs=1.0)
    assert found[0].region.height == pytest.approx(200.0, abs=1.0)
    # And it took no time at all: the page was never rendered.
    assert time.time() - started < 5.0


def test_an_ordinary_page_is_still_split(tmp_path) -> None:
    """A page that is not dense is found the usual way."""
    src = tmp_path / "panels.pdf"
    _two_panels(src)
    doc = fitz.open(str(src))
    try:
        content = comp._page_content_bytes(doc, doc[0])
    finally:
        doc.close()
    assert content < comp.DENSE_CONTENT_BYTES, content
    found = comp.detect_figures(src)
    assert len(found) == 2, found
    left, right = found
    assert left.region.x1 < right.region.x0


def test_the_content_of_a_page_is_read_without_rendering(tmp_path) -> None:
    """The size of a page is looked up, not drawn."""
    src = tmp_path / "panels.pdf"
    _two_panels(src)
    doc = fitz.open(str(src))
    try:
        started = time.time()
        content = comp._page_content_bytes(doc, doc[0])
        elapsed = time.time() - started
    finally:
        doc.close()
    assert content > 0
    assert elapsed < 1.0, elapsed


def test_a_whole_page_figure_is_embedded_whole(tmp_path) -> None:
    """A dense page is not cut out: it is embedded as it is.

    Cutting it out would mean drawing every one of its strokes, and a page
    of three million strokes takes a quarter of an hour to draw.
    """
    src = tmp_path / "page.pdf"
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.draw_rect(fitz.Rect(10, 10, 190, 190), color=(0, 0, 0),
                   fill=(0, 0, 0), width=1)
    doc.save(str(src))
    doc.close()
    figure = comp.Figure(path=src, page_index=0,
                         region=fitz.Rect(0, 0, 200, 200), whole_page=True)
    started = time.time()
    baked = comp.bake_figure(figure)
    elapsed = time.time() - started
    out = fitz.open(str(baked))
    try:
        assert abs(out[0].rect.width - 200) < 1, out[0].rect
        assert abs(out[0].rect.height - 200) < 1, out[0].rect
    finally:
        out.close()
    assert elapsed < 10.0, elapsed


def test_the_size_of_a_figure_does_not_need_the_page_drawn(tmp_path) -> None:
    """The material list asks for the size of every figure it holds."""
    src = tmp_path / "panels.pdf"
    _two_panels(src)
    figure = comp.Figure(path=src, page_index=0,
                         region=fitz.Rect(0, 0, 400, 200))
    started = time.time()
    width, height = comp.figure_display_size(figure)
    elapsed = time.time() - started
    assert width == pytest.approx(400.0, abs=1.0)
    assert height == pytest.approx(200.0, abs=1.0)
    assert elapsed < 5.0, elapsed
