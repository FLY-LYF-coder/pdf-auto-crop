"""The table, the array and the rubber band.

Three things the compositor learned to do: hold a table of cells, copy the
selection into a matrix of copies, and catch several objects at once with
a rubber band.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf as fitz  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402


def _table(**kwargs) -> comp.Table:
    data = dict(
        rect=fitz.Rect(40.0, 300.0, 400.0, 380.0),
        rows=[["材料", "模量"], ["钢", "210"], ["铝", "70"]],
    )
    data.update(kwargs)
    return comp.Table(**data)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_the_first_row_of_the_table_is_the_top_one() -> None:
    """A table is read from the top, the way it is written."""
    table = _table()
    top = table.cell_rect(0, 0)
    bottom = table.cell_rect(2, 0)
    assert top.y0 > bottom.y0, "the first row has to be the upper one"
    assert top.y1 <= table.rect.y1 + 0.01
    assert bottom.y0 >= table.rect.y0 - 0.01


def test_the_columns_share_the_width_they_are_given() -> None:
    """The weights say how wide every column is."""
    table = _table(col_weights=[2.0, 1.0])
    first = table.cell_rect(0, 0)
    second = table.cell_rect(0, 1)
    assert first.width == pytest.approx(second.width * 2, rel=0.01)
    assert first.x0 == pytest.approx(table.rect.x0)
    assert second.x1 == pytest.approx(table.rect.x1)


def test_the_three_line_table_has_three_lines() -> None:
    """A journal's table: a line on top, one under the head, one at the foot."""
    table = _table(style=comp.TABLE_THREE_LINE)
    rules = table.rules()
    assert len(rules) == 3, rules
    # The top rule, the thin one under the head, and the bottom rule.
    assert rules[0][1] == pytest.approx(table.rect.y1)
    assert rules[1][1] == pytest.approx(table.cell_rect(0, 0).y0)
    assert rules[2][1] == pytest.approx(table.rect.y0)
    assert rules[1][4] < rules[0][4], "the rule under the head is the thin one"


def test_the_grid_table_has_a_line_for_every_cell() -> None:
    table = _table(style=comp.TABLE_GRID)
    vertical = [r for r in table.rules() if r[0] == r[2]]
    horizontal = [r for r in table.rules() if r[1] == r[3]]
    # two columns and three rows: a line for every edge of every cell.
    assert len(vertical) == table.n_cols + 1
    assert len(horizontal) == table.n_rows + 1


def test_the_table_is_written_as_lines_and_as_text(tmp_path) -> None:
    """The table stays vector: lines are lines and the text is real text."""
    sheet = comp.Composition(width=842, height=595)
    sheet.tables.append(_table())
    out = tmp_path / "table.pdf"
    comp.export_composition(sheet, out, fit_to_content=False,
                            page_size=(842, 595))
    doc = fitz.open(str(out))
    try:
        page = doc[0]
        assert len(page.get_images()) == 0, "the table became a picture"
        assert len(page.get_drawings()) == 3, "the rules are missing"
        text = page.get_text()
        for cell in ("材料", "模量", "钢", "210", "铝", "70"):
            assert cell in text, text
        # The text sits inside the table, not somewhere else on the page.
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    assert span["bbox"][1] > 595 - 380 - 1
                    assert span["bbox"][3] < 595 - 300 + 1
    finally:
        doc.close()


def test_the_table_survives_a_save_and_a_load(tmp_path) -> None:
    """The layout file keeps the table as it was."""
    sheet = comp.Composition(width=842, height=595)
    table = _table(style=comp.TABLE_GRID, header=False, font_size=11.0,
                   align="left", col_weights=[1.5, 1.0])
    sheet.tables.append(table)
    back = comp.Composition.from_dict(sheet.to_dict())
    assert len(back.tables) == 1
    got = back.tables[0]
    assert got.rows == table.rows
    assert got.style == comp.TABLE_GRID
    assert got.header is False
    assert got.font_size == pytest.approx(11.0)
    assert got.align == "left"
    assert got.col_weights == table.col_weights
    assert got.rect == table.rect


def test_the_content_of_the_sheet_counts_the_table() -> None:
    """The table is part of the page: it has to be inside it."""
    sheet = comp.Composition(width=842, height=595)
    sheet.tables.append(_table())
    bounds = sheet.content_bounds()
    assert bounds is not None
    assert bounds.x0 == pytest.approx(40.0)
    assert bounds.y1 == pytest.approx(380.0)


def test_the_sheet_lists_the_table_among_its_objects() -> None:
    """The rubber band looks at every kind of object."""
    sheet = comp.Composition(width=842, height=595)
    sheet.tables.append(_table())
    boxes = sheet.object_boxes()
    kinds = {kind for kind, _, _ in boxes}
    assert "table" in kinds
    tables = [rect for kind, _, rect in boxes if kind == "table"]
    assert tables[0].x0 == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# The array
# ---------------------------------------------------------------------------


def test_the_cells_of_the_array_are_filled_in_reading_order() -> None:
    """The first cell is at the top left, the last one at the bottom right."""
    cells = comp.array_positions(6, 3, 2, 100.0, 50.0, 10.0, 0.0, 0.0)
    assert len(cells) == 6
    first, second, third = cells[0], cells[1], cells[2]
    # The first row: three cells side by side, all at the same height.
    assert first[1] == pytest.approx(second[1])
    assert second[1] == pytest.approx(third[1])
    assert first[0] < second[0] < third[0]
    # The second row stands below the first one.
    assert cells[3][1] < first[1]
    # The gap is the gap.
    assert second[0] - first[0] == pytest.approx(110.0)


def test_the_array_stops_when_the_cells_run_out() -> None:
    assert len(comp.array_positions(5, 2, 2, 10.0, 10.0, 0.0, 0.0, 0.0)) == 4


def test_the_array_of_one_cell_is_the_cell_itself() -> None:
    assert comp.array_positions(1, 1, 1, 10.0, 20.0, 0.0, 5.0, 6.0) == [
        (5.0, 6.0)]


# ---------------------------------------------------------------------------
# The rubber band
# ---------------------------------------------------------------------------


def test_the_band_catches_what_it_sweeps_over() -> None:
    """Everything the band touches is caught."""
    items = [
        ("figure", 0, fitz.Rect(10, 10, 50, 50)),
        ("shape", 1, fitz.Rect(100, 100, 140, 140)),
        ("text", 2, fitz.Rect(200, 200, 240, 220)),
        ("figure", 3, fitz.Rect(300, 300, 340, 340)),
    ]
    caught = comp.select_in_band(fitz.Rect(0, 0, 150, 150), items)
    assert caught.get("figure") == [0]
    assert caught.get("shape") == [1]
    assert "text" not in caught
    assert 3 not in caught.get("figure", [])


def test_the_band_catches_an_object_it_only_touches() -> None:
    """A cursor that sweeps over a figure catches it, as it should."""
    items = [("figure", 0, fitz.Rect(40, 40, 200, 200))]
    caught = comp.select_in_band(fitz.Rect(0, 0, 60, 60), items)
    assert caught.get("figure") == [0]


def test_a_band_that_catches_nothing_catches_nothing() -> None:
    items = [("figure", 0, fitz.Rect(40, 40, 200, 200))]
    assert comp.select_in_band(fitz.Rect(0, 0, 10, 10), items) == {}
