"""Giving the labels of an old export the face the canvas shows.

The old build wrote the labels with the PDF base-14 faces; the reader draws
those with whatever Times it has.  The migration writes them again in the
face the canvas draws with -- the very file, embedded -- and the figures
must not notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402
from PIL import Image, ImageChops  # noqa: E402

from pdf_auto_crop import migrate  # noqa: E402


def _old_export(path: Path) -> Path:
    """A file like the old build wrote: a figure and built-in labels."""
    src = path.with_name("source.pdf")
    doc = fitz.open()
    page = doc.new_page(width=200, height=150)
    page.draw_rect(fitz.Rect(20, 20, 180, 120), color=(0, 0, 0), width=1)
    page.insert_text(fitz.Point(30, 40), "axis label", fontsize=8)
    doc.save(str(src))
    doc.close()

    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    figure = fitz.open(str(src))
    try:
        page.show_pdf_page(fitz.Rect(40, 100, 240, 220), figure, 0)
    finally:
        figure.close()
    # The label, written the way the old build wrote it.
    page.insert_text(fitz.Point(60, 90), "(a)", fontsize=10, fontname="tibo")
    page.insert_text(fitz.Point(260, 90), "(b)", fontsize=10, fontname="tibo")
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()
    return path


def _spans(path: Path):
    doc = fitz.open(str(path))
    try:
        out = []
        for page in doc:
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        if span["text"].strip():
                            out.append((span["font"], span["size"],
                                        span["text"]))
        return out
    finally:
        doc.close()


def _render(path: Path) -> Image.Image:
    doc = fitz.open(str(path))
    try:
        pix = doc[0].get_pixmap(dpi=150)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Finding the labels
# ---------------------------------------------------------------------------


def test_the_old_labels_are_found(tmp_path) -> None:
    path = _old_export(tmp_path / "old.pdf")
    doc = fitz.open(str(path))
    try:
        labels = migrate.find_labels(doc)
    finally:
        doc.close()
    assert [label.text for label in labels] == ["(a)", "(b)"]
    assert labels[0].face == "Times-Bold"
    assert labels[0].size == pytest.approx(10.0)
    # The baseline is where the exporter put it: PyMuPDF counts downwards.
    assert labels[0].origin.x == pytest.approx(60.0, abs=0.5)
    assert labels[0].origin.y == pytest.approx(90.0, abs=1.0)


def _cropped_export(path: Path) -> Path:
    """A file whose page was cropped: the content keeps the old coordinates.

    The crop takes a part of the page out and the content stays where it
    was, so the label sits at coordinates that have nothing to do with the
    visible page.  Reading them the wrong way puts the label outside.
    """
    src = path.with_name("source.pdf")
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.draw_rect(fitz.Rect(20, 20, 280, 380), color=(0, 0, 0), width=1)
    doc.save(str(src))
    doc.close()

    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    figure = fitz.open(str(src))
    try:
        page.show_pdf_page(fitz.Rect(40, 150, 240, 350), figure, 0)
    finally:
        figure.close()
    page.insert_text(fitz.Point(60, 320), "(a)", fontsize=10, fontname="tibo")
    # The crop takes the lower part of the page away.
    page.set_cropbox(fitz.Rect(20, 60, 280, 340))
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()
    return path


def test_a_cropped_page_is_read_in_its_own_frame(tmp_path) -> None:
    path = _cropped_export(tmp_path / "cropped.pdf")
    doc = fitz.open(str(path))
    try:
        labels = migrate.find_labels(doc)
        assert len(labels) == 1
        origin = labels[0].origin
        # The label has to stay inside the visible page.
        assert 0 <= origin.x <= doc[0].rect.width
        assert 0 <= origin.y <= doc[0].rect.height
    finally:
        doc.close()


def test_the_label_of_a_cropped_page_stays_where_it_was(tmp_path) -> None:
    src = _cropped_export(tmp_path / "cropped.pdf")
    dst = tmp_path / "cropped_tnr.pdf"
    count, problems = migrate.migrate_file(src, dst)
    assert count == 1
    assert problems == []

    spans = _spans(dst)
    labels = [s for s in spans if s[2] == "(a)"]
    assert len(labels) == 1, spans
    assert "Times New Roman Bold" in labels[0][0]

    # The label must be where it was: the same place on the page.
    before = _render(src)
    after = _render(dst)
    difference = ImageChops.difference(before, after)
    box = difference.getbbox()
    assert box is not None
    assert 0 < box[0] < before.width, box
    assert 0 < box[1] < before.height, box


def test_a_file_without_old_labels_has_none(tmp_path) -> None:
    path = tmp_path / "plain.pdf"
    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    page.insert_text((20, 50), "hello", fontsize=12)
    doc.save(str(path))
    doc.close()
    doc = fitz.open(str(path))
    try:
        assert migrate.find_labels(doc) == []
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Rewriting them
# ---------------------------------------------------------------------------


def test_the_labels_are_written_in_the_real_font(tmp_path) -> None:
    src = _old_export(tmp_path / "old.pdf")
    dst = tmp_path / "old_tnr.pdf"
    count, problems = migrate.migrate_file(src, dst)
    assert count == 2
    assert problems == []
    assert dst.exists()

    doc = fitz.open(str(dst))
    try:
        names = [f[3] for f in doc[0].get_fonts(full=True)]
    finally:
        doc.close()
    assert any("Times New Roman Bold" in name for name in names), names

    spans = _spans(dst)
    labels = [s for s in spans if s[2] in ("(a)", "(b)")]
    assert len(labels) == 2
    assert all("Times New Roman Bold" in s[0] for s in labels), labels
    assert all(s[1] == pytest.approx(10.0) for s in labels)


def test_the_figure_is_not_touched(tmp_path) -> None:
    src = _old_export(tmp_path / "old.pdf")
    dst = tmp_path / "old_tnr.pdf"
    migrate.migrate_file(src, dst)

    before = [s for s in _spans(src) if s[2] != "(a)" and s[2] != "(b)"]
    after = [s for s in _spans(dst) if s[2] != "(a)" and s[2] != "(b)"]
    assert before == after
    assert any("axis label" in s[2] for s in after)


def test_only_the_labels_change_on_the_page(tmp_path) -> None:
    src = _old_export(tmp_path / "old.pdf")
    dst = tmp_path / "old_tnr.pdf"
    migrate.migrate_file(src, dst)

    before = _render(src)
    after = _render(dst)
    assert before.size == after.size
    difference = ImageChops.difference(before, after)
    box = difference.getbbox()
    assert box is not None, "nothing changed at all"

    # The change has to be where the labels are, and nowhere else.
    doc = fitz.open(str(src))
    try:
        boxes = []
        for block in doc[0].get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for s in line["spans"]:
                    if s["text"] in ("(a)", "(b)"):
                        boxes.append(s["bbox"])
    finally:
        doc.close()
    scale = 150 / 72.0
    left = int(min(b[0] for b in boxes) * scale) - 4
    top = int(min(b[1] for b in boxes) * scale) - 4
    right = int(max(b[2] for b in boxes) * scale) + 4
    bottom = int(max(b[3] for b in boxes) * scale) + 4
    assert box[0] >= left and box[1] >= top, (box, (left, top))
    assert box[2] <= right and box[3] <= bottom, (box, (right, bottom))


def test_the_original_is_left_alone(tmp_path) -> None:
    src = _old_export(tmp_path / "old.pdf")
    before = src.read_bytes()
    dst = tmp_path / "old_tnr.pdf"
    migrate.migrate_file(src, dst)
    assert src.read_bytes() == before


def test_a_tree_is_written_beside_the_originals(tmp_path) -> None:
    folder = tmp_path / "out"
    folder.mkdir()
    _old_export(folder / "one.pdf")
    _old_export(folder / "two.pdf")
    results = migrate.migrate_tree(folder)
    assert len(results) == 2
    assert (folder / "one_tnr.pdf").exists()
    assert (folder / "two_tnr.pdf").exists()
    # The originals are still there, untouched.
    assert (folder / "one.pdf").exists()
    assert (folder / "two.pdf").exists()


def test_a_file_without_old_labels_is_not_written(tmp_path) -> None:
    path = tmp_path / "plain.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=100)
    doc.save(str(path))
    doc.close()
    dst = tmp_path / "plain_tnr.pdf"
    count, problems = migrate.migrate_file(path, dst)
    assert count == 0
    assert not dst.exists()
