"""The sheet is a frame around the content: it carries it when it moves."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402


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


def _move_content(comp_obj, dx: float, dy: float) -> None:
    """What the panel does when the sheet's edge is dragged."""
    for placement in comp_obj.placements:
        rect = placement.rect
        placement.rect = fitz.Rect(rect.x0 + dx, rect.y0 + dy,
                                   rect.x1 + dx, rect.y1 + dy)
    for shape in comp_obj.shapes:
        shape.start = fitz.Point(shape.start.x + dx, shape.start.y + dy)
        shape.end = fitz.Point(shape.end.x + dx, shape.end.y + dy)
    for item in comp_obj.texts:
        item.position = fitz.Point(item.position.x + dx, item.position.y + dy)


def test_a_sheet_that_moved_still_exports_its_content(tmp_path) -> None:
    """The page is the sheet, so the content has to come with it.

    A sheet whose edge was dragged carries the figures with it -- otherwise
    they end up outside the page and the export comes out blank.
    """
    sheet = comp.Composition(width=400, height=300)
    sheet.placements.append(comp.Placement(
        _figure(tmp_path), rect=fitz.Rect(40, 120, 240, 220), label="(a)"))

    # the sheet's left edge is dragged 60 points to the right
    sheet_x, sheet_y = 60.0, 0.0
    _move_content(sheet, sheet_x, sheet_y)

    out = tmp_path / "moved.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(400, 300),
                            offset=(-sheet_x, -sheet_y))
    doc = fitz.open(str(out))
    try:
        page = doc[0]
        pix = page.get_pixmap(dpi=100)
        ink = sum(1 for i in range(0, len(pix.samples), 3)
                  if pix.samples[i] < 200)
        assert ink > 100, "the page came out blank"
        labels = [s["text"] for b in page.get_text("dict")["blocks"]
                  if b.get("type") == 0 for l in b.get("lines", [])
                  for s in l["spans"] if s["text"].strip()]
        assert labels == ["(a)"], labels
    finally:
        doc.close()


def test_the_content_keeps_its_place_on_the_page(tmp_path) -> None:
    """Moving the sheet must not move the figures on the page."""
    sheet = comp.Composition(width=400, height=300)
    sheet.placements.append(comp.Placement(
        _figure(tmp_path), rect=fitz.Rect(40, 120, 240, 220), label="(a)"))

    plain = tmp_path / "plain.pdf"
    comp.export_composition(sheet, plain, fit_to_content=False,
                            page_size=(400, 300), offset=(0.0, 0.0))

    _move_content(sheet, 60.0, 0.0)
    moved = tmp_path / "moved.pdf"
    comp.export_composition(sheet, moved, fit_to_content=False,
                            page_size=(400, 300), offset=(-60.0, 0.0))

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

    assert label_box(plain) == label_box(moved)


def test_an_export_that_came_out_empty_is_written_again(tmp_path) -> None:
    """The export checks itself: an empty page is written again."""
    sheet = comp.Composition(width=400, height=300)
    sheet.placements.append(comp.Placement(
        _figure(tmp_path), rect=fitz.Rect(40, 120, 240, 220), label="(a)"))
    out = tmp_path / "out.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(400, 300))
    assert comp._content_on_the_page(out, sheet)


@pytest.mark.parametrize("offset", [(0.0, 0.0), (-60.0, -30.0)])
def test_the_page_holds_the_content_whatever_the_sheet_does(tmp_path,
                                                            offset) -> None:
    sheet = comp.Composition(width=400, height=300)
    sheet.placements.append(comp.Placement(
        _figure(tmp_path), rect=fitz.Rect(40, 120, 240, 220), label="(a)"))
    out = tmp_path / "out.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(400, 300), offset=offset)
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=100)
        ink = sum(1 for i in range(0, len(pix.samples), 3)
                  if pix.samples[i] < 200)
        assert ink > 100
    finally:
        doc.close()
