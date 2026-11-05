"""The page background, the formulas and the saved layout.

Three things the compositor learned to do: paint the page the way the sheet
looks, typeset a formula whatever way it is written, and keep the layout of
the sheet in a file that can be opened again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402
from pdf_auto_crop import tex  # noqa: E402


# ---------------------------------------------------------------------------
# The page background
# ---------------------------------------------------------------------------


def _corner(path: Path) -> tuple[int, int, int, int]:
    """The colour of the top left corner of the page, with its alpha."""
    doc = fitz.open(str(path))
    try:
        pix = doc[0].get_pixmap(dpi=72, alpha=True)
        return tuple(pix.samples[0:4])
    finally:
        doc.close()


def _sheet() -> comp.Composition:
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.shapes.append(comp.Shape(
        comp.SHAPE_RECT, fitz.Point(20, 20), fitz.Point(120, 120),
        color=(0, 0, 0), width=1.0, fill=True,
        fill_color=(1.0, 1.0, 1.0), fill_opacity=0.25))
    return sheet


def test_the_page_is_white_the_way_the_sheet_is(tmp_path) -> None:
    """The sheet is white on the canvas, so the page is white too."""
    out = tmp_path / "white.pdf"
    comp.export_composition(_sheet(), out, fit_to_content=False,
                            page_size=(200.0, 200.0))
    red, green, blue, alpha = _corner(out)
    assert (red, green, blue, alpha) == (255, 255, 255, 255)


def test_the_page_can_be_left_transparent(tmp_path) -> None:
    """A page with no background lets whatever is behind it through."""
    out = tmp_path / "clear.pdf"
    comp.export_composition(_sheet(), out, fit_to_content=False,
                            page_size=(200.0, 200.0),
                            white_background=False)
    red, green, blue, alpha = _corner(out)
    assert alpha == 0, f"{red},{green},{blue},{alpha}"


def test_a_white_fill_covers_what_is_under_it(tmp_path) -> None:
    """A white box that is opaque hides what it stands on."""
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.shapes.append(comp.Shape(
        comp.SHAPE_RECT, fitz.Point(20, 20), fitz.Point(120, 120),
        color=(0, 0, 0), width=1.0, fill=True,
        fill_color=(1.0, 1.0, 1.0), fill_opacity=1.0))
    out = tmp_path / "opaque.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(200.0, 200.0))
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=72, alpha=True)
        i = (100 * pix.width + 70) * 4
        assert tuple(pix.samples[i:i + 4]) == (255, 255, 255, 255)
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# The formulas
# ---------------------------------------------------------------------------


def test_every_way_of_writing_a_formula_is_the_same() -> None:
    """``$...$``, ``\\[...\\]`` and ``$$...$$`` are one formula."""
    first = tex.render_math("$E = mc^2$", 12.0)
    for text in ("\\[E = mc^2\\]", "$$E = mc^2$$"):
        other = tex.render_math(text, 12.0)
        assert other is not None, text
        assert other == first, text


@pytest.mark.parametrize("text", ["$E = mc^2$", "\\[E = mc^2\\]", "$$E = mc^2$$"])
def test_the_delimiters_never_reach_the_file(text, tmp_path) -> None:
    """The formula is typeset, and its own delimiters stay out of it."""
    sheet = comp.Composition(width=300.0, height=200.0)
    sheet.texts.append(comp.TextItem(text=text, position=fitz.Point(40, 60),
                                     size=14.0, formula=True))
    out = tmp_path / "formula.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(300.0, 200.0))
    doc = fitz.open(str(out))
    try:
        page = doc[0]
        written = page.get_text().strip()
        assert "$" not in written, written
        assert "\\" not in written, written
        spans = [s for b in page.get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]]
        assert spans, "the formula is not on the page"
        # It stands where the text was put.
        assert spans[0]["origin"][0] == pytest.approx(40.0, abs=0.5)
        assert spans[0]["origin"][1] == pytest.approx(140.0, abs=0.5)
    finally:
        doc.close()


def test_the_two_dollar_signs_are_one_formula() -> None:
    """``$$...$$`` is a formula, not two empty ones."""
    parts = tex.split_math("$$E = mc^2$$")
    assert parts == [("$$E = mc^2$$", True)]
    assert tex.math_parts("$$E = mc^2$$") is not None


@pytest.mark.parametrize("text", ["$E = mc^2$", "\\[E = mc^2\\]", "$$E = mc^2$$"])
def test_a_caption_may_hold_a_formula(text, tmp_path) -> None:
    """The caption of a graphic element may be mathematics too."""
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.shapes.append(comp.Shape(
        comp.SHAPE_RECT, fitz.Point(40, 40), fitz.Point(160, 120),
        color=(0, 0, 0), width=1.0, text=text, text_size=14.0, formula=True))
    out = tmp_path / "caption.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(200.0, 200.0))
    doc = fitz.open(str(out))
    try:
        page = doc[0]
        written = page.get_text().strip()
        assert "$" not in written, written
        assert "\\" not in written, written
        assert len(page.get_images()) == 0, "the formula became a picture"
    finally:
        doc.close()


def test_a_formula_is_recognised_however_it_is_written() -> None:
    """The user does not have to say that the text is mathematics."""
    assert tex.looks_like_math("$x^2$")
    assert tex.looks_like_math("\\[x^2\\]")
    assert tex.looks_like_math("$$x^2$$")
    assert not tex.looks_like_math("x^2")


def test_the_brackets_are_not_drawn_as_letters() -> None:
    """The delimiters are the user's, not the typesetter's."""
    rendered = tex.render_math("\\[x^2\\]", 12.0)
    assert rendered is not None
    doc = fitz.open(str(rendered))
    try:
        text = doc[0].get_text().strip()
        assert "[" not in text and "]" not in text, text
    finally:
        doc.close()


def test_a_formula_goes_into_the_file_as_vector(tmp_path) -> None:
    """A formula is written as mathematics, not as a picture."""
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.texts.append(comp.TextItem(text="$E = mc^2$",
                                     position=fitz.Point(40, 60),
                                     formula=True))
    out = tmp_path / "formula.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(200.0, 200.0))
    doc = fitz.open(str(out))
    try:
        page = doc[0]
        assert len(page.get_images()) == 0, "the formula became a picture"
        assert page.get_text().strip(), "the formula is not text"
    finally:
        doc.close()


def test_a_formula_lands_where_the_text_stands(tmp_path) -> None:
    """The formula sits on the baseline the text was put on."""
    sheet = comp.Composition(width=300.0, height=200.0)
    sheet.texts.append(comp.TextItem(text="$E = mc^2$",
                                     position=fitz.Point(100, 50),
                                     size=20.0, formula=True))
    out = tmp_path / "formula.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(300.0, 200.0))
    doc = fitz.open(str(out))
    try:
        spans = [s for b in doc[0].get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]]
        assert spans, "the formula is not on the page"
        origin = spans[0]["origin"]
        assert origin[0] == pytest.approx(100.0, abs=0.5), origin
        assert origin[1] == pytest.approx(150.0, abs=0.5), origin
    finally:
        doc.close()


def test_a_text_may_hold_words_and_mathematics(tmp_path) -> None:
    """The words are written as text and the mathematics is typeset."""
    sheet = comp.Composition(width=300.0, height=200.0)
    sheet.texts.append(comp.TextItem(text="公式：$\\times 10$",
                                     position=fitz.Point(40, 60),
                                     size=14.0, formula=True))
    out = tmp_path / "mixed.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(300.0, 200.0))
    doc = fitz.open(str(out))
    try:
        page = doc[0]
        text = page.get_text()
        assert "公式" in text, text
        assert "10" in text, text
        spans = [s for b in page.get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]]
        faces = {s["font"] for s in spans}
        # The words are written in a text face, the mathematics in the face
        # of the mathematics.
        assert len(faces) >= 2, faces
        assert len(page.get_images()) == 0, "the formula became a picture"
    finally:
        doc.close()


def test_a_price_is_not_a_formula() -> None:
    """Two dollar signs around a price are ordinary text."""
    assert not tex.looks_like_math("价格 $5 和 $10")
    assert tex.math_parts("价格 $5 和 $10") is None


def test_a_formula_is_recognised_inside_a_text() -> None:
    """The formula does not have to be the whole text."""
    assert tex.looks_like_math("公式：$\\times 10$")
    parts = tex.math_parts("公式：$\\times 10$")
    assert parts is not None
    assert parts[0] == ("公式：", False)
    assert parts[1][1] is True


def test_a_turned_shape_keeps_its_own_shape(tmp_path) -> None:
    """A rectangle that is turned is still a rectangle."""
    shape = comp.Shape(comp.SHAPE_RECT, fitz.Point(40, 40),
                       fitz.Point(120, 100), color=(0, 0, 0), width=1.0,
                       fill=True, fill_color=(1.0, 0.0, 0.0),
                       fill_opacity=1.0, rotation=45.0)
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.shapes.append(shape)
    out = tmp_path / "turned.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(200.0, 200.0))
    doc = fitz.open(str(out))
    try:
        drawings = doc[0].get_drawings()
        # The page itself is painted white; the shape is what is left.
        shape_drawings = [d for d in drawings
                          if any(it[0] in ("l", "qu") for it in d["items"])]
        assert shape_drawings, "the shape was not drawn"
        kinds = {it[0] for d in shape_drawings for it in d["items"]}
        # Four lines make a turned rectangle; one rectangle would be the box
        # around it.
        assert "l" in kinds or "qu" in kinds, kinds
        assert "re" not in kinds, kinds
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# The layout
# ---------------------------------------------------------------------------


def _figure(name: str = "f") -> comp.Figure:
    return comp.Figure(path=Path("probe.pdf"), page_index=0,
                       region=fitz.Rect(0, 0, 10, 10), name=name)


def test_the_layout_keeps_everything(tmp_path) -> None:
    """The sheet, the material list and the layers all come back."""
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.placements.append(comp.Placement(figure=_figure(),
                                            rect=fitz.Rect(0, 0, 10, 10),
                                            label="(a)"))
    sheet.shapes.append(comp.Shape(comp.SHAPE_RECT, fitz.Point(20, 20),
                                   fitz.Point(40, 40), fill=True))
    sheet.tables.append(comp.Table(rect=fitz.Rect(50, 50, 80, 70)))
    sheet.texts.append(comp.TextItem(text="note",
                                     position=fitz.Point(90, 90)))
    sheet.palette.append(_figure("a"))
    sheet.palette.append(_figure("b"))
    sheet.restack([("shape", 0)], "top")

    path = tmp_path / "layout.json"
    comp.save_composition(sheet, path)
    back = comp.load_composition(path)

    assert len(back.placements) == 1
    assert back.placements[0].label == "(a)"
    assert len(back.shapes) == 1
    assert back.shapes[0].fill is True
    assert len(back.tables) == 1
    assert len(back.texts) == 1
    assert len(back.palette) == 2
    assert back.stack_order() == sheet.stack_order()
    assert back.width == pytest.approx(sheet.width)
    assert back.height == pytest.approx(sheet.height)


def test_a_layout_without_a_material_list_still_opens(tmp_path) -> None:
    """An old file has no material list in it: it opens all the same."""
    sheet = comp.Composition(width=200.0, height=200.0)
    sheet.shapes.append(comp.Shape(comp.SHAPE_RECT, fitz.Point(20, 20),
                                   fitz.Point(40, 40)))
    path = tmp_path / "old.json"
    comp.save_composition(sheet, path)
    data = path.read_text(encoding="utf-8").replace(
        '"palette": [],', "")
    path.write_text(data, encoding="utf-8")
    back = comp.load_composition(path)
    assert back.palette == []
    assert len(back.shapes) == 1
