"""A turned text: the sheet and the file must show it in the same place.

Turning a text is the one thing the canvas cannot do by itself, so the very
PDF the export writes is rendered and shown.  When the two do not agree --
the text stands in a different place, or it is turned the other way -- the
user only finds out when the file is made, which is too late.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tkinter as tk  # noqa: E402
import pymupdf as fitz  # noqa: E402
from PIL import Image  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ink_box(path: Path) -> fitz.Rect:
    """The box the ink of the page takes, in points."""
    doc = fitz.open(str(path))
    try:
        page = doc[0]
        pix = page.get_pixmap(dpi=72)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        grey = img.convert("L").point(lambda v: 255 if v < 250 else 0)
        box = grey.getbbox()
        if box is None:
            return fitz.Rect(0, 0, 0, 0)
        return fitz.Rect(*box)
    finally:
        doc.close()


def _export(c: comp.Composition, path: Path) -> Path:
    return comp.export_composition(
        c, path, fit_to_content=False, page_size=(c.width, c.height))


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


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def test_a_turned_text_starts_where_it_was_put(tmp_path) -> None:
    """The text is turned about its own start, not about its middle."""
    c = comp.Composition(width=400.0, height=400.0)
    c.texts.append(comp.TextItem(
        text="Hello", position=fitz.Point(100, 300), size=14.0,
        rotation=90.0))
    ink = _ink_box(_export(c, tmp_path / "out.pdf"))
    # The text starts at the place the user put it and runs downwards.
    assert ink.x0 == pytest.approx(100.0, abs=3.0)
    assert ink.y0 == pytest.approx(100.0, abs=3.0)
    # It is turned: it is tall and narrow now.
    assert ink.height > ink.width


def test_a_turned_text_with_mathematics_is_turned_too(tmp_path) -> None:
    """A text that holds words and mathematics is turned as well.

    It used to be written out in parts, and the turn was quietly dropped:
    the file showed the text lying flat while the sheet showed it standing.
    """
    c = comp.Composition(width=400.0, height=400.0)
    c.texts.append(comp.TextItem(
        text="note $x^2$", position=fitz.Point(100, 300), size=14.0,
        rotation=90.0, formula=True))
    ink = _ink_box(_export(c, tmp_path / "out.pdf"))
    assert ink.height > ink.width, "the text was not turned in the file"


def test_a_turned_text_stays_where_it_was_put(tmp_path) -> None:
    """The turn does not move the text away from its place."""
    c = comp.Composition(width=400.0, height=400.0)
    c.texts.append(comp.TextItem(
        text="Hello", position=fitz.Point(100, 300), size=14.0,
        rotation=90.0))
    ink = _ink_box(_export(c, tmp_path / "out.pdf"))
    assert ink.x0 == pytest.approx(100.0, abs=3.0)
    assert ink.y1 <= 400.0


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------


def test_the_sheet_shows_the_turned_text_where_the_file_puts_it(
        tmp_path) -> None:
    """What the canvas shows and what the file holds are the same thing."""
    root, panel = _panel()
    try:
        item = comp.TextItem(
            text="Hello", position=fitz.Point(100, 300), size=14.0,
            rotation=90.0)
        c = comp.Composition(width=400.0, height=400.0)
        c.texts.append(item)
        panel.composition.texts.append(item)
        panel._redraw()
        panel.update()

        box = panel._draw_turned_text(0, item)
        assert box is not None, "the turned text was not drawn"
        left, top, right, bottom = box
        # The box on the canvas, in the sheet's own frame.
        ux0, uy1 = panel._to_user(left, top)
        ux1, uy0 = panel._to_user(right, bottom)
        shown = fitz.Rect(ux0, uy0, ux1, uy1)

        # The same text in the file, in the sheet's own frame.
        ink = _ink_box(_export(c, tmp_path / "out.pdf"))
        written = fitz.Rect(ink.x0, 400.0 - ink.y1, ink.x1, 400.0 - ink.y0)

        assert shown.x0 == pytest.approx(written.x0, abs=3.0)
        assert shown.y0 == pytest.approx(written.y0, abs=3.0)
        assert shown.x1 == pytest.approx(written.x1, abs=3.0)
        assert shown.y1 == pytest.approx(written.y1, abs=3.0)
    finally:
        panel.destroy()
        root.destroy()


def test_the_sheet_shows_the_turned_text_the_other_way_round(tmp_path) -> None:
    """A text turned the other way is turned the other way on the sheet."""
    root, panel = _panel()
    try:
        item = comp.TextItem(
            text="Hello", position=fitz.Point(100, 300), size=14.0,
            rotation=270.0)
        c = comp.Composition(width=400.0, height=400.0)
        c.texts.append(item)
        panel.composition.texts.append(item)
        panel._redraw()
        panel.update()

        box = panel._draw_turned_text(0, item)
        assert box is not None
        left, top, right, bottom = box
        ux0, uy1 = panel._to_user(left, top)
        ux1, uy0 = panel._to_user(right, bottom)
        shown = fitz.Rect(ux0, uy0, ux1, uy1)

        ink = _ink_box(_export(c, tmp_path / "out.pdf"))
        written = fitz.Rect(ink.x0, 400.0 - ink.y1, ink.x1, 400.0 - ink.y0)
        assert shown.x0 == pytest.approx(written.x0, abs=3.0)
        assert shown.y0 == pytest.approx(written.y0, abs=3.0)
        assert shown.x1 == pytest.approx(written.x1, abs=3.0)
        assert shown.y1 == pytest.approx(written.y1, abs=3.0)
    finally:
        panel.destroy()
        root.destroy()
