"""The face in the export has to be the face on the canvas.

The canvas draws with the fonts Windows has -- Times New Roman, Arial,
Courier New, SimSun.  The export used to write the built-in "Times-Roman",
which the reader draws with whatever Times it has: a different face from the
one the user arranged, and a different one from the body of a thesis set in
Times New Roman.

These tests hold the export to the file the canvas shows.  The sharpest one
compares the pixels: the exported label and a reference written by the same
engine from the same file have to be *identical*, and they have to differ
from the built-in face.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402
from PIL import Image, ImageChops  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402
from pdf_auto_crop import fonts  # noqa: E402

DPI = 300


def _build_source(tmp_path: Path):
    src = tmp_path / "src.pdf"
    doc = fitz.open()
    page = doc.new_page(width=120, height=80)
    page.draw_rect(fitz.Rect(10, 10, 110, 70), color=(0, 0, 0), width=1)
    doc.save(str(src))
    doc.close()
    return src, fitz.Rect(0, 0, 120, 80)


def _first_span(page: fitz.Page):
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"].strip():
                    return span
    return None


def _export_with_label(tmp_path: Path, **kwargs) -> Path:
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=200, height=220)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(40, 90, 160, 170), label="(a) Sample", label_size=12.0,
        **kwargs))
    return comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(200, 220))


def _render(pdf: Path, clip: fitz.Rect) -> bytes:
    doc = fitz.open(str(pdf))
    try:
        pix = doc[0].get_pixmap(dpi=DPI, clip=clip)
        return pix.samples
    finally:
        doc.close()


def _reference(tmp_path: Path, origin, size: float, fontfile: str,
               fontname: str, page_size=(200, 220)) -> Path:
    """The same text, written by the same engine from the same file."""
    path = tmp_path / f"ref_{Path(fontfile).stem or fontname}.pdf"
    doc = fitz.open()
    page = doc.new_page(width=page_size[0], height=page_size[1])
    page.insert_text(fitz.Point(origin), "(a) Sample", fontsize=size,
                     fontname=fontname, fontfile=fontfile or None)
    doc.save(str(path))
    doc.close()
    return path


# ---------------------------------------------------------------------------
# The font module
# ---------------------------------------------------------------------------


def test_the_name_of_a_face_carries_its_own_style() -> None:
    assert fonts.style_of("Times-Roman") == "regular"
    assert fonts.style_of("Times-Bold") == "bold"
    assert fonts.style_of("Times-Italic") == "italic"
    assert fonts.style_of("Times-Bold Italic") == "bolditalic"
    assert fonts.style_of("Helvetica-Oblique") == "italic"
    # The switches of the user come on top of the name.
    assert fonts.style_of("Times-Roman", bold=True) == "bold"
    assert fonts.style_of("Times-Roman", italic=True) == "italic"
    assert fonts.style_of("Times-Roman", bold=True, italic=True) == "bolditalic"
    # A name that carries its own style keeps it.
    assert fonts.style_of("Times-Bold", bold=False) == "bold"


def test_a_face_is_looked_up_in_the_system_fonts() -> None:
    path = fonts.face_file("Times-Roman")
    if path is None:
        pytest.skip("this machine has no Times New Roman file")
    assert Path(path).is_file()
    assert Path(path).name.lower() == "times.ttf"
    # The bold variant is a file of its own.
    bold = fonts.face_file("Times-Roman", bold=True)
    if bold:
        assert Path(bold).name.lower() == "timesbd.ttf"


def test_a_face_that_is_not_there_is_none() -> None:
    assert fonts.face_file("No Such Face") is None
    assert fonts.cjk_file() is None or Path(fonts.cjk_file()).is_file()


def test_the_glyphs_of_a_file_can_be_asked_for() -> None:
    path = fonts.face_file("Times-Roman")
    if path is None:
        pytest.skip("this machine has no Times New Roman file")
    assert fonts.has_glyphs(path, "(a) Sample")
    assert not fonts.has_glyphs(path, "图一")


# ---------------------------------------------------------------------------
# The export
# ---------------------------------------------------------------------------


def test_the_export_carries_the_face_the_canvas_shows(tmp_path) -> None:
    """The exported label is the very file the canvas draws with.

    The two renderings have to be identical, pixel for pixel: same engine,
    same file, same baseline, same size.  A built-in face would differ.
    """
    file_path = fonts.face_file("Times-Roman")
    if file_path is None:
        pytest.skip("this machine has no Times New Roman file")

    out = _export_with_label(tmp_path)
    doc = fitz.open(str(out))
    try:
        span = _first_span(doc[0])
        assert span is not None
        assert span["text"] == "(a) Sample"
        origin = span["origin"]
        size = span["size"]
        clip = fitz.Rect(span["bbox"])
        page_size = (doc[0].rect.width, doc[0].rect.height)
    finally:
        doc.close()

    same = _reference(tmp_path, origin, size, file_path, "TNRRef", page_size)
    other = _reference(tmp_path, origin, size, "", "tiro", page_size)

    pixels = _render(out, clip)
    assert pixels == _render(same, clip), "the export is not the file itself"
    assert pixels != _render(other, clip), "the export is the built-in face"


def test_the_export_embeds_the_font_rather_than_naming_it(tmp_path) -> None:
    """A built-in face is named; a real one is carried along."""
    file_path = fonts.face_file("Times-Roman")
    if file_path is None:
        pytest.skip("this machine has no Times New Roman file")

    out = _export_with_label(tmp_path)
    doc = fitz.open(str(out))
    try:
        names = [f[3] for f in doc[0].get_fonts(full=True)]
        expected = fitz.Font(fontfile=file_path).name
        assert any(expected in name for name in names), names
        # The built-in Times is not there any more.
        assert not any(name.startswith("Times-") for name in names), names
    finally:
        doc.close()


def test_the_embedded_font_is_a_subset(tmp_path) -> None:
    """A whole Times New Roman is a third of a megabyte; a caption is not."""
    file_path = fonts.face_file("Times-Roman")
    if file_path is None:
        pytest.skip("this machine has no Times New Roman file")

    out = _export_with_label(tmp_path)
    assert out.stat().st_size < Path(file_path).stat().st_size / 2


def test_without_the_font_file_the_built_in_face_is_used(tmp_path,
                                                         monkeypatch) -> None:
    """A machine that has no font file still gets a label."""
    monkeypatch.setattr(fonts, "_find", lambda name: None)

    name, fontfile = comp.resolve_label_font("(a)", "Times-Roman")
    assert fontfile == ""
    assert name == "tiro"

    out = _export_with_label(tmp_path)
    doc = fitz.open(str(out))
    try:
        span = _first_span(doc[0])
        assert span is not None
        assert "Times" in span["font"]
    finally:
        doc.close()


def test_a_font_file_the_user_picked_is_used_as_it_is(tmp_path) -> None:
    """The user's own file wins over the built-in face."""
    file_path = fonts.face_file("Times-Bold")
    if file_path is None:
        pytest.skip("this machine has no Times New Roman file")

    out = _export_with_label(tmp_path, label_font="Times-Bold",
                             label_fontfile=file_path)
    doc = fitz.open(str(out))
    try:
        span = _first_span(doc[0])
        assert span is not None
        expected = fitz.Font(fontfile=file_path).name
        assert expected in span["font"]
    finally:
        doc.close()


def test_a_font_that_cannot_write_the_text_is_not_used(tmp_path) -> None:
    """A label written with .notdef is an invisible label.

    A font can *hold* a character and still be unable to write it -- the
    full-width brackets are in Times New Roman's map, but the encoder puts
    .notdef in the file for them.  The face has to be asked, not consulted.
    """
    path = fonts.face_file("Times-Bold")
    if path is None:
        pytest.skip("this machine has no Times New Roman file")
    assert fonts.has_glyphs(path, "（a）") or not fonts.can_write(path, "（a）")
    assert not fonts.can_write(path, "（a）")
    assert not fonts.can_write(path, "自然")

    # So the label is written in a face that can carry it.
    name, file = comp.resolve_label_font("（a）", "Times-Bold")
    assert file != path
    assert comp.font_renders("（a）", name, file)


def test_the_font_file_the_user_picked_is_checked_too(tmp_path) -> None:
    """A file of their own is no excuse for an empty label."""
    path = fonts.face_file("Times-Bold")
    if path is None:
        pytest.skip("this machine has no Times New Roman file")
    name, file = comp.resolve_label_font("自然", "", path)
    assert file != path, "a font that cannot write the text was used"
    assert comp.font_renders("自然", name, file)


def test_the_label_is_there_in_the_export(tmp_path) -> None:
    """A label the face cannot write must still be on the page."""
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=400, height=300)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(40, 120, 340, 280), label="（a）", label_size=12.0))
    out = comp.export_composition(c, tmp_path / "out.pdf", fit_to_content=False,
                                  page_size=(400, 300))
    doc = fitz.open(str(out))
    try:
        text = doc[0].get_text()
    finally:
        doc.close()
    assert "（a）" in text, repr(text)
    # And there has to be ink where the label is.
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=200, clip=fitz.Rect(100, 155, 260, 185))
    finally:
        doc.close()
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    grey = image.convert("L")
    ink = ImageChops.difference(grey, Image.new("L", grey.size, 255))
    assert sum(1 for p in ink.getdata() if p > 40) > 20


def test_the_export_checks_that_the_labels_survived(tmp_path,
                                                     monkeypatch) -> None:
    """If the labels do not come out, the sheet is written again.

    The built-in faces are the last resort: every reader has them, and they
    always write the text.
    """
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=400, height=300)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(40, 120, 340, 280), label="(a)", label_size=12.0))

    path = fonts.face_file("Times-Roman")
    if path is None:
        pytest.skip("this machine has no Times New Roman file")
    real = comp.resolve_label_font

    def broken(text, font="", fontfile="", bold=False, italic=False):
        # The first export writes the label with a face that cannot carry
        # it; the second one falls back to the built-in faces.
        if comp._BUILTIN_FACES:
            return real(text, font, fontfile, bold, italic)
        return ("brokenfont", path)

    monkeypatch.setattr(comp, "resolve_label_font", broken)

    out = comp.export_composition(c, tmp_path / "out.pdf", fit_to_content=False,
                                  page_size=(400, 300))
    doc = fitz.open(str(out))
    try:
        text = doc[0].get_text()
    finally:
        doc.close()
    assert "(a)" in text, repr(text)


def test_the_sheet_is_written_again_when_the_fonts_are_mangled(tmp_path,
                                                               monkeypatch) -> None:
    """The subsetting can lose a label; the export has to notice.

    The face that carries the label is kept: the fonts are simply written
    whole, which costs a few kilobytes and keeps the letters the user
    arranged.
    """
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=400, height=300)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(40, 120, 340, 280), label="(a)", label_size=12.0))

    def mangle(doc):
        """Stand in for a subsetting that loses the label."""
        for page in doc:
            for xref in page.get_contents():
                data = doc.xref_stream(xref)
                if b"TJ" not in data or b"times" not in data:
                    continue
                doc.update_stream(xref, data.replace(b"<000b0044000c>",
                                                     b"<000000000000>"))

    monkeypatch.setattr(comp, "subset_fonts", mangle)

    out = comp.export_composition(c, tmp_path / "out.pdf", fit_to_content=False,
                                  page_size=(400, 300))
    doc = fitz.open(str(out))
    try:
        text = doc[0].get_text()
        names = [f[3] for f in doc[0].get_fonts(full=True)]
    finally:
        doc.close()
    assert "(a)" in text, repr(text)
    # The face that carries the label is still the one the canvas shows.
    assert any("Times New Roman" in name for name in names), names


def test_a_font_without_the_glyphs_is_not_used(tmp_path) -> None:
    """The check is the encoder's answer, not the font's character map."""
    path = fonts.face_file("Times-Roman")
    if path is None:
        pytest.skip("this machine has no Times New Roman file")
    assert fonts.can_write(path, "(a) Sample")
    assert not fonts.can_write(path, "（a）")
    """The family Tk draws with has to be the family the file holds.

    Otherwise the canvas shows one face and the export carries another, and
    the two drift apart the moment somebody renames one of them.
    """
    from pdf_auto_crop import compositor_gui as gui

    pairs = {
        "Times-Roman": ("Times New Roman", "times.ttf"),
        "Helvetica": ("Arial", "arial.ttf"),
        "Courier": ("Courier New", "cour.ttf"),
        "宋体 (中文)": ("SimSun", "simsun.ttc"),
    }
    for face, (family, file_name) in pairs.items():
        assert gui.TK_FONTS[face] == family
        path = fonts.face_file(face)
        if path is None:
            continue
        assert Path(path).name.lower() == file_name
        # The file has to belong to the family Tk draws with.
        internal = fitz.Font(fontfile=path).name
        assert internal.lower().startswith(family.lower()), internal
