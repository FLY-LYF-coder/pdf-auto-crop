"""The white background of an imported figure.

A figure that comes out of a plotting library carries its own white
paper: matplotlib paints the whole page white before it draws anything.
That white is ink in the content stream, so it hides whatever the figure
is put on top of.

The fix has to stay vector -- the whole point of this tool is that the
export is still a drawing and not a picture.  So the white is not
coloured out but *not painted*: the painting operator of a background
fill is rewritten, one byte, and everything else is left alone.

These tests are about the finding and the rewriting.  The rules are
deliberately narrow: a background is a white fill that covers a good
part of the figure, not a white legend box a few per cent big.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402
from PIL import Image  # noqa: E402

from pdf_auto_crop import whiten  # noqa: E402
from pdf_auto_crop import compositor as comp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _page_with(tmp_path: Path, ops: str, width: int = 200, height: int = 200,
               name: str = "fig.pdf") -> Path:
    """A page whose content stream is exactly ``ops``."""
    doc = fitz.open()
    try:
        page = doc.new_page(width=width, height=height)
        # A fresh page has no content stream of its own; one is needed
        # before it can be written into.
        page.draw_rect(fitz.Rect(0, 0, 1, 1), color=(0, 0, 0), width=0.1)
        xref = page.get_contents()[0]
        doc.update_stream(xref, ops.encode("latin-1"), compress=False)
        path = tmp_path / name
        doc.save(str(path))
    finally:
        doc.close()
    return path


def _stream_of(path: Path) -> bytes:
    doc = fitz.open(str(path))
    try:
        xref = doc[0].get_contents()[0]
        return doc.xref_stream(xref)
    finally:
        doc.close()


def _corner_alpha(path: Path) -> int:
    """The transparency of the top-left corner of the page."""
    doc = fitz.open(str(path))
    try:
        pix = doc[0].get_pixmap(dpi=36, alpha=True)
        img = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        return img.getchannel("A").getpixel((1, 1))
    finally:
        doc.close()


def _fills(path: Path, region=None, **kw):
    doc = fitz.open(str(path))
    try:
        page = doc[0]
        data = doc.xref_stream(page.get_contents()[0])
        return whiten.scan_white_fills(page, data, region, **kw)
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Finding the background
# ---------------------------------------------------------------------------


def test_a_white_page_is_found(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f")
    fills = _fills(path)
    assert len(fills) == 1
    assert fills[0].op == "f"
    assert fills[0].frac == pytest.approx(1.0, abs=0.01)


def test_the_page_background_is_found_in_gray_too(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f")
    assert len(_fills(path)) == 1


def test_white_in_cyan_black_and_yellow_is_found(tmp_path):
    # CMYK white is 0 0 0 0.
    path = _page_with(tmp_path, "0 0 0 0 k 0 0 200 200 re f")
    assert len(_fills(path)) == 1


def test_a_white_fill_in_rgb_is_found(tmp_path):
    path = _page_with(tmp_path, "1 1 1 rg 0 0 200 200 re f")
    assert len(_fills(path)) == 1


def test_a_coloured_background_is_left_alone(tmp_path):
    path = _page_with(tmp_path, "0 0 1 rg 0 0 200 200 re f")
    assert _fills(path) == []


def test_a_pattern_is_not_mistaken_for_a_colour(tmp_path):
    path = _page_with(tmp_path, "/Pattern cs /P0 scn 0 0 200 200 re f")
    assert _fills(path) == []


def test_a_letter_f_in_a_string_is_not_an_operator(tmp_path):
    path = _page_with(tmp_path, "BT (fff f) Tj ET 1 g 0 0 200 200 re f")
    fills = _fills(path)
    assert len(fills) == 1


def test_the_graphics_state_stack_is_kept(tmp_path):
    # Inside the q the fill is blue, so the page is not white there.
    path = _page_with(tmp_path, "q 0 0 1 rg 0 0 200 200 re f Q")
    assert _fills(path) == []


def test_a_colour_set_before_the_bracket_is_used(tmp_path):
    path = _page_with(tmp_path, "1 g q 0 0 1 rg 0 0 200 200 re f Q")
    assert _fills(path) == []


def test_a_small_white_box_is_not_a_background(tmp_path):
    # A white legend box on a black page: too small to be the background.
    path = _page_with(tmp_path, "0 0 0 rg 0 0 200 200 re f 1 g 10 10 20 20 re f")
    assert _fills(path) == []


def test_a_big_white_box_on_a_black_page_is_a_background(tmp_path):
    path = _page_with(tmp_path, "0 0 0 rg 0 0 200 200 re f "
                                "1 g 0 0 200 150 re f")
    fills = _fills(path)
    assert len(fills) == 1
    assert fills[0].frac == pytest.approx(0.75, abs=0.02)


def test_the_area_is_measured_against_the_figure_not_the_page(tmp_path):
    # A white page on the left half; the figure is the whole page, so the
    # white hides half of it.
    path = _page_with(tmp_path, "1 g 0 0 100 200 re f")
    whole = fitz.Rect(0, 0, 200, 200)
    assert _fills(path, region=whole)[0].frac == pytest.approx(0.5, abs=0.02)
    # The same white hides nothing of a figure on the right half.
    right = fitz.Rect(100, 0, 200, 200)
    assert _fills(path, region=right) == []


def test_a_path_made_of_lines_is_found(tmp_path):
    # matplotlib writes the background as a polygon, not as a rectangle.
    path = _page_with(tmp_path, "1 g 0 0 m 200 0 l 200 200 l 0 200 l h f")
    fills = _fills(path)
    assert len(fills) == 1
    assert fills[0].frac == pytest.approx(1.0, abs=0.01)


def test_a_stroke_and_fill_becomes_a_stroke(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re B")
    fills = _fills(path)
    assert len(fills) == 1
    assert fills[0].op == "B"


def test_a_fill_and_close_becomes_a_close_and_stroke(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re b")
    fills = _fills(path)
    assert len(fills) == 1
    assert fills[0].op == "b"


def test_a_clip_is_not_a_fill(tmp_path):
    # q ... re W n draws a clip; n must not be read as a fill.
    path = _page_with(tmp_path, "q 0 0 200 200 re W n 1 g 0 0 200 200 re f Q")
    fills = _fills(path)
    assert len(fills) == 1


# ---------------------------------------------------------------------------
# Rewriting
# ---------------------------------------------------------------------------


def test_the_fill_stops_painting(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f")
    doc = fitz.open(str(path))
    try:
        page = doc[0]
        data = doc.xref_stream(page.get_contents()[0])
        fills = whiten.scan_white_fills(page, data, None)
        patched = whiten.patch_fills(data, fills)
        assert patched.endswith(b"n")
        assert len(patched) == len(data)
    finally:
        doc.close()


def test_the_rest_of_the_stream_is_left_alone(tmp_path):
    ops = "1 g 0 0 200 200 re f 0 0 1 rg 20 20 60 60 re f"
    path = _page_with(tmp_path, ops)
    data = _stream_of(path)
    doc = fitz.open(str(path))
    try:
        page = doc[0]
        fills = whiten.scan_white_fills(page, data, None)
        patched = whiten.patch_fills(data, fills)
    finally:
        doc.close()
    changed = [i for i in range(len(data)) if data[i] != patched[i]]
    assert len(changed) == 1
    assert patched[:changed[0]] == data[:changed[0]]
    assert patched[changed[0] + 1:] == data[changed[0] + 1:]


def test_a_stroke_and_fill_keeps_its_stroke(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re B")
    data = _stream_of(path)
    doc = fitz.open(str(path))
    try:
        fills = whiten.scan_white_fills(doc[0], data, None)
        patched = whiten.patch_fills(data, fills)
    finally:
        doc.close()
    assert patched.endswith(b"S")
    assert len(patched) == len(data)


def test_a_two_byte_operator_keeps_the_length(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re B*")
    data = _stream_of(path)
    doc = fitz.open(str(path))
    try:
        fills = whiten.scan_white_fills(doc[0], data, None)
        patched = whiten.patch_fills(data, fills)
    finally:
        doc.close()
    assert len(patched) == len(data)
    assert patched.endswith(b"S ")


# ---------------------------------------------------------------------------
# The whole page
# ---------------------------------------------------------------------------


def test_the_page_becomes_transparent(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f 0 0 1 rg 20 20 60 60 re f")
    assert _corner_alpha(path) == 255
    doc = fitz.open(str(path))
    try:
        result = whiten.strip_page_background(doc, doc[0], None)
        assert result.removed == 1
        out = tmp_path / "stripped.pdf"
        doc.save(str(out))
    finally:
        doc.close()
    assert _corner_alpha(out) == 0


def test_the_drawing_survives_the_strip(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f "
                                "0 0 1 rg 50 50 100 100 re f")
    doc = fitz.open(str(path))
    try:
        whiten.strip_page_background(doc, doc[0], None)
        out = tmp_path / "stripped.pdf"
        doc.save(str(out))
    finally:
        doc.close()
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=36, alpha=True)
        img = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        rgb = img.convert("RGB")
        # the middle of the blue box
        point = rgb.getpixel((img.width // 2, img.height // 2))
        assert point[2] > 200 and point[0] < 80
    finally:
        doc.close()


def test_a_page_without_white_is_left_alone(tmp_path):
    path = _page_with(tmp_path, "0 0 1 rg 0 0 200 200 re f")
    doc = fitz.open(str(path))
    try:
        result = whiten.strip_page_background(doc, doc[0], None)
        assert result.removed == 0
        assert result.note
    finally:
        doc.close()


def test_a_huge_stream_is_skipped(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f")
    doc = fitz.open(str(path))
    try:
        result = whiten.strip_page_background(doc, doc[0], None, max_bytes=10)
        assert result.removed == 0
        assert "太大" in result.note
    finally:
        doc.close()


def test_the_original_file_is_not_touched(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f")
    before = path.read_bytes()
    doc = fitz.open(str(path))
    try:
        whiten.strip_page_background(doc, doc[0], None)
    finally:
        doc.close()
    assert path.read_bytes() == before


def test_the_stripped_page_is_still_vector(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f 0 0 1 rg 20 20 60 60 re f")
    doc = fitz.open(str(path))
    try:
        whiten.strip_page_background(doc, doc[0], None)
        out = tmp_path / "stripped.pdf"
        doc.save(str(out))
    finally:
        doc.close()
    doc = fitz.open(str(out))
    try:
        # a picture would come as an image; a drawing comes as paths
        assert len(doc[0].get_images()) == 0
        assert len(doc[0].get_drawings()) >= 1
    finally:
        doc.close()


def test_a_page_whose_origin_is_not_at_the_corner(tmp_path):
    # The page's own origin sits away from the corner of the frame.
    doc = fitz.open()
    try:
        page = doc.new_page(width=200, height=200)
        page.set_mediabox(fitz.Rect(10, 20, 210, 220))
        page.set_cropbox(fitz.Rect(50, 50, 150, 150))
        page.draw_rect(fitz.Rect(0, 0, 1, 1), color=(0, 0, 0), width=0.1)
        xref = page.get_contents()[0]
        doc.update_stream(xref, b"1 g 50 50 100 100 re f", compress=False)
        path = tmp_path / "offset.pdf"
        doc.save(str(path))
    finally:
        doc.close()
    fills = _fills(path)
    assert len(fills) == 1
    assert fills[0].frac == pytest.approx(1.0, abs=0.02)


# ---------------------------------------------------------------------------
# The baked figure
# ---------------------------------------------------------------------------


def _figure(path: Path, **kw) -> comp.Figure:
    return comp.Figure(path=path, page_index=0,
                       region=fitz.Rect(0, 0, 200, 200), **kw)


def test_a_baked_figure_loses_its_white(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f "
                                "0 0 1 rg 50 50 100 100 re f")
    baked = comp.bake_figure(_figure(path, transparent_bg=True),
                             cache_dir=tmp_path / "cache")
    assert _corner_alpha(baked) == 0


def test_a_baked_figure_keeps_its_white_by_default(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f "
                                "0 0 1 rg 50 50 100 100 re f")
    baked = comp.bake_figure(_figure(path), cache_dir=tmp_path / "cache")
    assert _corner_alpha(baked) == 255


def test_the_baked_figure_keeps_its_drawing(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f "
                                "0 0 1 rg 50 50 100 100 re f")
    baked = comp.bake_figure(_figure(path, transparent_bg=True),
                             cache_dir=tmp_path / "cache")
    doc = fitz.open(str(baked))
    try:
        assert len(doc[0].get_images()) == 0
        assert len(doc[0].get_drawings()) >= 1
    finally:
        doc.close()


def test_the_two_kinds_of_figure_are_cached_apart(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f")
    plain = _figure(path)
    clear = _figure(path, transparent_bg=True)
    assert plain.key != clear.key


def test_the_flag_survives_a_saved_layout(tmp_path):
    path = _page_with(tmp_path, "1 g 0 0 200 200 re f")
    figure = _figure(path, transparent_bg=True)
    again = comp.Figure.from_dict(figure.to_dict())
    assert again.transparent_bg is True
    assert again.key == figure.key
