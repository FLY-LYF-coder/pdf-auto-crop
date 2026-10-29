"""The sheet is the frame the figures stand in.

Dragging one of its edges moves that edge and nothing else: the figures
stay where they are, and the frame is not allowed to lose them either --
a page that no longer holds them would come out of the export blank.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402
from pdf_auto_crop import compositor_gui  # noqa: E402


def _figure(tmp_path: Path):
    src = tmp_path / "fig.pdf"
    doc = fitz.open()
    page = doc.new_page(width=200, height=150)
    page.draw_rect(fitz.Rect(20, 20, 180, 130), color=(0.1, 0.3, 0.8),
                    fill=(0.1, 0.3, 0.8), width=0)
    doc.save(str(src))
    doc.close()
    return comp.Figure(path=src, page_index=0,
                       region=fitz.Rect(0, 0, 200, 150), name="fig")


def _sheet(tmp_path: Path) -> comp.Composition:
    sheet = comp.Composition(width=400, height=300)
    sheet.placements.append(comp.Placement(
        _figure(tmp_path), rect=fitz.Rect(40, 120, 240, 220), label="(a)"))
    return sheet


def _drag_sheet(sheet, left, bottom, right, top, handle, dx, dy):
    """What the panel does when a grip is dragged.

    The function the panel really uses is called, not a copy of it: the
    sheet's edges are moved in the composition's frame, and an edge that is
    pulled inwards stops at the content rather than cutting it off.
    """
    return compositor_gui.resized_sheet(
        left, bottom, right - left, top - bottom, handle, dx, dy,
        sheet.content_bounds())


def test_the_figures_do_not_move_when_the_sheet_is_resized(tmp_path) -> None:
    """The frame moves; the figures stay."""
    sheet = _sheet(tmp_path)
    before = fitz.Rect(sheet.placements[0].rect)

    # the right edge is dragged to the right
    left, bottom, width, height = _drag_sheet(sheet, 0, 0, 400, 300, 3,
                                              40.0, 0.0)
    assert width == 440
    assert fitz.Rect(sheet.placements[0].rect) == before


def test_the_view_does_not_follow_the_sheet() -> None:
    """The view is measured against its own frame.

    If it followed the sheet, a page that is resized would rescale
    everything under the user's eyes -- the figures would jump about even
    though they had not moved.
    """
    import inspect

    from pdf_auto_crop import compositor_gui

    source = inspect.getsource(compositor_gui.ComposePanel._redraw)
    assert "self._view_w" in source
    assert "self.sheet_w * pp" not in source
    # and the mapping is measured against the view, not against the sheet
    mapping = inspect.getsource(compositor_gui.ComposePanel._to_canvas)
    assert "self._view_x" in mapping
    assert "self.sheet_x" not in mapping


def test_the_edge_follows_the_mouse(tmp_path) -> None:
    """The edge that was grabbed is the one that moves."""
    sheet = _sheet(tmp_path)

    # the top edge is dragged up
    left, bottom, width, height = _drag_sheet(sheet, 0, 0, 400, 300, 1,
                                              0.0, 30.0)
    assert height == 330
    assert bottom == 0

    # the left edge is dragged to the right
    left, bottom, width, height = _drag_sheet(sheet, 0, 0, 400, 300, 7,
                                              20.0, 0.0)
    assert left == 20
    assert width == 380


def test_an_edge_stops_at_the_content(tmp_path) -> None:
    """The page may not lose the figures."""
    sheet = _sheet(tmp_path)
    # the content is between x=40 and x=240
    left, bottom, width, height = _drag_sheet(sheet, 0, 0, 400, 300, 7,
                                              300.0, 0.0)
    assert left == 40, "the edge cut the content off"
    assert right_is_inside(left, width)


def right_is_inside(left: float, width: float) -> bool:
    return left + width >= 240


def test_the_edges_that_were_not_dragged_do_not_move(tmp_path) -> None:
    """Only the edge under the mouse follows it."""
    sheet = _sheet(tmp_path)

    # the top edge is dragged up
    left, bottom, width, height = _drag_sheet(sheet, 0, 0, 400, 300, 1,
                                              0.0, 30.0)
    assert (left, bottom) == (0, 0)
    assert (width, height) == (400, 330)

    # the left edge is dragged to the right
    left, bottom, width, height = _drag_sheet(sheet, 0, 0, 400, 300, 7,
                                              20.0, 0.0)
    assert (left, bottom) == (20, 0)
    assert (width, height) == (380, 300)


def test_an_edge_that_is_pulled_inwards_does_not_make_the_page_bigger(
        tmp_path) -> None:
    """Content that hangs over the page may not grow the page.

    A label that stands below the page is a thing the user arranged; the
    page may not swallow the edge it hangs over when the user pulls the
    other way.
    """
    sheet = _sheet(tmp_path)
    # the label of the figure stands below the page
    sheet.placements[0].rect = fitz.Rect(40, 5, 240, 105)
    sheet.placements[0].label_mode = comp.LABEL_BELOW
    bounds = sheet.content_bounds()
    assert bounds.y0 < 0, "the label is supposed to hang over"

    # the bottom edge is pulled up: the page may not grow downwards
    left, bottom, width, height = _drag_sheet(sheet, 0, 0, 400, 300, 5,
                                              0.0, 40.0)
    assert bottom == 0, "the page grew instead of shrinking"
    assert height == 300


def test_the_grips_are_looked_for_where_the_sheet_is_drawn() -> None:
    """The grips have to be where they are drawn.

    The sheet does not have to sit in the view's frame any more -- a page
    that has been made bigger no longer does -- so the grips are looked
    for on the sheet itself.  Were they looked for in the old place, the
    page could not be resized any more.
    """
    import inspect

    hit = inspect.getsource(compositor_gui.ComposePanel._hit_sheet_handle)
    assert "_sheet_rect" in hit
    assert "_origin" not in hit


def test_the_export_is_never_blank(tmp_path) -> None:
    """Whatever the sheet does, the page holds the figures."""
    sheet = _sheet(tmp_path)
    # the sheet is dragged to the left, away from the figures
    sheet_x, sheet_y = -60.0, 0.0
    out = tmp_path / "out.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(400, 300),
                            offset=(-sheet_x, -sheet_y))
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=100)
        # The samples are fetched once: PyMuPDF copies the whole buffer every
        # time the property is read.
        samples = pix.samples
        ink = sum(1 for i in range(0, len(samples), 3)
                  if samples[i] < 200)
        assert ink > 100, "the page came out blank"
        labels = [s["text"] for b in doc[0].get_text("dict")["blocks"]
                  if b.get("type") == 0 for l in b.get("lines", [])
                  for s in l["spans"] if s["text"].strip()]
        assert labels == ["(a)"], labels
    finally:
        doc.close()


def test_the_content_sits_where_the_frame_says(tmp_path) -> None:
    """The page is the sheet, so the sheet's edge decides where things land.

    The figures keep their place in the composition's frame; the page moves
    under them, and the export writes them where the frame says.
    """
    sheet = _sheet(tmp_path)
    plain = tmp_path / "plain.pdf"
    comp.export_composition(sheet, plain, fit_to_content=False,
                            page_size=(400, 300), offset=(0.0, 0.0))

    # the sheet stands 60 points to the left of the frame
    moved = tmp_path / "moved.pdf"
    comp.export_composition(sheet, moved, fit_to_content=False,
                            page_size=(400, 300), offset=(60.0, 0.0))

    def label_box(path):
        doc = fitz.open(str(path))
        try:
            for block in doc[0].get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        if span["text"].strip():
                            return tuple(round(v, 1) for v in span["bbox"])
        finally:
            doc.close()
        return None

    a = label_box(plain)
    b = label_box(moved)
    # the page's left edge moved 60 points to the left, so the figures sit
    # 60 points further right on the page -- and no further.
    assert b[0] == pytest.approx(a[0] + 60.0, abs=0.5)
    assert b[1] == pytest.approx(a[1], abs=0.5)


@pytest.mark.parametrize("offset", [(0.0, 0.0), (-60.0, -30.0)])
def test_the_page_holds_the_content_whatever_the_sheet_does(tmp_path,
                                                            offset) -> None:
    sheet = _sheet(tmp_path)
    out = tmp_path / "out.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(400, 300), offset=offset)
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=100)
        samples = pix.samples
        ink = sum(1 for i in range(0, len(samples), 3)
                  if samples[i] < 200)
        assert ink > 100
    finally:
        doc.close()
