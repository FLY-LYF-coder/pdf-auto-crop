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

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402

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


def test_the_canvas_and_the_export_agree_on_the_face() -> None:
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
