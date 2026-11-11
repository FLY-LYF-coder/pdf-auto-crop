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
