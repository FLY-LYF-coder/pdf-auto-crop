"""A formula: where it stands on the sheet, and what stands behind it.

Two things were wrong with the formulas.  The page a formula is written on
carried a sheet of white paper of its own, so the formula came with a white
box around it -- on the sheet and in the file, where it covered whatever was
underneath.  And the formula did not stand where it was put: the sheet and
the file disagreed about the place.
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


def _alpha_box(path: Path) -> fitz.Rect:
    """The box the ink of the page takes, in points.

    The page is read with its transparency, so the paper of the page does
    not count as ink.
    """
    doc = fitz.open(str(path))
    try:
        page = doc[0]
        pix = page.get_pixmap(dpi=72, alpha=True)
        img = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        box = img.getchannel("A").getbbox()
        if box is None:
            return fitz.Rect(0, 0, 0, 0)
        return fitz.Rect(*box)
    finally:
        doc.close()


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
# The page a formula is written on
# ---------------------------------------------------------------------------


def test_a_formula_is_written_on_transparent_paper() -> None:
    """The page holds the mathematics and nothing else.

    The formula used to come with a sheet of white paper of its own, which
    showed on the canvas as a white box and covered whatever was under the
    formula in the file.
    """
    from pdf_auto_crop.tex import render_math

    rendered = render_math("$x^2$", 14.0)
    assert rendered is not None
    doc = fitz.open(str(rendered))
    try:
        pix = doc[0].get_pixmap(dpi=72, alpha=True)
        img = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        alpha = img.getchannel("A")
        # There is paper of its own: every pixel would be opaque.
        assert alpha.getextrema()[0] == 0, "the page is not transparent"
    finally:
        doc.close()


def test_a_formula_does_not_paint_a_white_box_over_the_page(tmp_path) -> None:
    """A formula in the file does not paint a white box over the page."""
    c = comp.Composition(width=400.0, height=400.0)
    c.texts.append(comp.TextItem(
        text="$x^2$", position=fitz.Point(100, 200), size=14.0,
        formula=True))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(400, 400),
        white_background=False)
    doc = fitz.open(str(out))
    try:
        whites = [d for d in doc[0].get_drawings()
                  if d.get("fill") and all(v > 0.99 for v in d["fill"])]
        assert not whites, "the formula painted a white box over the page"
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Where the formula stands
# ---------------------------------------------------------------------------


def test_the_sheet_puts_a_formula_where_the_file_puts_it(tmp_path) -> None:
    """The sheet and the file agree about where a formula stands."""
    root, panel = _panel()
    try:
        item = comp.TextItem(
            text="$x^2$", position=fitz.Point(150, 200), size=14.0,
            formula=True)
        c = comp.Composition(width=400.0, height=400.0)
        c.texts.append(item)
        panel.composition.texts.append(item)
        panel._redraw()
        panel.update()

        box = panel._draw_formula(
            "formula", item.text, item.size, item.position, ("text", "0"))
        assert box is not None, "the formula was not drawn"
        left, top, right, bottom = box
        # The box on the canvas, in the sheet's own frame.
        ux0, uy1 = panel._to_user(left, top)
        ux1, uy0 = panel._to_user(right, bottom)
        shown = fitz.Rect(ux0, uy0, ux1, uy1)

        # The same formula in the file, in the sheet's own frame.
        out = comp.export_composition(
            c, tmp_path / "out.pdf", fit_to_content=False,
            page_size=(400, 400), white_background=False)
        ink = _alpha_box(out)
        written = fitz.Rect(ink.x0, 400.0 - ink.y1, ink.x1, 400.0 - ink.y0)

        assert shown.x0 == pytest.approx(written.x0, abs=3.0)
        assert shown.y0 == pytest.approx(written.y0, abs=3.0)
        assert shown.x1 == pytest.approx(written.x1, abs=3.0)
        assert shown.y1 == pytest.approx(written.y1, abs=3.0)
    finally:
        panel.destroy()
        root.destroy()


def test_the_baseline_of_a_formula_starts_at_its_place() -> None:
    """The formula is put where the text was put, not below it."""
    root, panel = _panel()
    try:
        anchor = fitz.Point(150, 200)
        box = panel._draw_formula(
            "formula", "$x^2$", 14.0, anchor, ("text", "0"))
        assert box is not None
        left, top, right, bottom = box
        # The bottom of a formula is its baseline: it must stand on the
        # place the text was put.
        _, base = panel._to_user(left, bottom)
        assert base == pytest.approx(anchor.y, abs=2.0)
    finally:
        panel.destroy()
        root.destroy()
