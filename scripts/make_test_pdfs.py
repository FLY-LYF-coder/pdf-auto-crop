"""Generate a battery of synthetic test PDFs for pdf-auto-crop.

Each case is hand-crafted to exercise a specific scenario the algorithm must
handle correctly:

* case1: single figure, lots of whitespace           -> 1 detected crop.
* case2: 2x2 grid of figures                          -> 4 detected crops.
* case3: complex finite-element mesh + arrows + text   -> 1 detected crop.
* case4: two figures with a wide (25 mm) gap          -> 2 detected crops.
* case5: two figures with a tight (3 mm) gap          -> 2 detected crops.
* case6: multi-page (1 + 4 + 2 figures)               -> 7 detected crops.
* case7: vector-only figure (verifies preservation)   -> 1 detected crop.

Diagnostic cases (D1-D8) live in ``tests/fixtures_diag/`` and are designed
to probe **known weak spots** of the current detection algorithm:

* D1: 1x3 horizontal row, 15mm gap (basic sanity for row layout)
* D2: 3+1 asymmetric (top row small, bottom row big)  - mixed row widths
* D3: 1 big left + 2x2 grid right (very asymmetric L layout)
* D4: dense FEM mesh with blank interior (large empty area inside)
* D5: topology optimization (irregular filled polygons, may bridge)
* D6: 2 figures 0.5mm apart (below all preset merge thresholds)
* D7: mixed gaps - 20mm and 1mm vertically stacked
* D8: 1 row, 3 figures of different sizes (30, 60, 30 mm wide)

All figures are drawn with ``page.draw_*`` and ``page.insert_text`` so that
the output contains real vector primitives -- not raster patches.
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Tuple

import pymupdf as fitz


FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FIXTURES.mkdir(parents=True, exist_ok=True)

FIXTURES_DIAG = Path(__file__).resolve().parent.parent / "tests" / "fixtures_diag"
FIXTURES_DIAG.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# A4 dimensions in points (1 mm = 2.83464567 pt; A4 = 210 x 297 mm).
A4_W_PT = 210 * 72 / 25.4
A4_H_PT = 297 * 72 / 25.4
MM = 72 / 25.4  # millimetre -> point factor


def _new_blank_a4() -> fitz.Document:
    doc = fitz.open()
    doc.insert_page(0, width=A4_W_PT, height=A4_H_PT)
    return doc


def _draw_grid(page: fitz.Rect, rect: fitz.Rect, *, color=(0, 0, 0), width: float = 0.6) -> None:
    """Draw a thin black rectangle outline -- useful for marking figure bounds."""
    page.draw_rect(rect, color=color, width=width)


def _draw_text(page: fitz.Rect, pos: Tuple[float, float], text: str, size: float = 10) -> None:
    page.insert_text(pos, text, fontsize=size, color=(0, 0, 0))


# ---------------------------------------------------------------------------
# Case 1 -- single figure, big white margin.
def make_case1_single_figure() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    # Place figure centred on page with ~30 mm white border all around.
    fig_w = 150 * MM
    fig_h = 100 * MM
    x0 = (A4_W_PT - fig_w) / 2
    y0 = (A4_H_PT - fig_h) / 2
    rect = fitz.Rect(x0, y0, x0 + fig_w, y0 + fig_h)

    page.draw_rect(rect, color=(0, 0, 0), width=0.8)
    # Some interior content.
    inner = fitz.Rect(x0 + 5 * MM, y0 + 5 * MM, x0 + fig_w - 5 * MM, y0 + fig_h - 5 * MM)
    page.draw_rect(inner, color=(0.2, 0.2, 0.2), width=0.4)
    page.insert_text(
        (x0 + 10 * MM, y0 + 15 * MM),
        "Figure 1: A single figure with whitespace margins.",
        fontsize=11,
    )
    # A few decorative rectangles.
    for i in range(3):
        bx = x0 + (15 + i * 35) * MM
        by = y0 + 25 * MM
        page.draw_rect(fitz.Rect(bx, by, bx + 25 * MM, by + 25 * MM), color=(0, 0, 0.7), width=0.5)

    out = FIXTURES / "case1_single_figure.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# Case 2 -- 2x2 grid (each cell is its own figure; no enclosing border).
def make_case2_four_figures() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    cell_w = 70 * MM
    cell_h = 80 * MM
    h_gap = 20 * MM
    v_gap = 30 * MM
    total_w = 2 * cell_w + h_gap
    total_h = 2 * cell_h + v_gap
    start_x = (A4_W_PT - total_w) / 2
    start_y = (A4_H_PT - total_h) / 2

    for row in range(2):
        for col in range(2):
            cx0 = start_x + col * (cell_w + h_gap)
            cx1 = cx0 + cell_w
            if row == 0:
                # Upper row: bottom edge below, top edge above.
                rect_y0 = start_y + v_gap + cell_h
                rect_y1 = start_y + total_h
            else:
                rect_y0 = start_y
                rect_y1 = start_y + cell_h
            page.draw_rect(
                fitz.Rect(cx0, rect_y0, cx1, rect_y1),
                color=(0, 0, 0),
                width=0.6,
            )
            label = f"({row + 1},{col + 1})"
            page.insert_text(
                (cx0 + 4 * MM, rect_y1 - 4 * MM),
                label,
                fontsize=10,
                color=(0, 0, 0),
            )
            # A few internal lines per cell so they look like real figures.
            for t in range(3):
                ly = rect_y0 + (10 + t * 12) * MM  # below bottom edge + offset
                page.draw_line(
                    (cx0 + 4 * MM, ly),
                    (cx1 - 4 * MM, ly),
                    color=(0.3, 0.3, 0.3),
                    width=0.4,
                )

    out = FIXTURES / "case2_four_figures.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# Case 3 -- finite-element mesh with axes, arrows, text.
def make_case3_fem_mesh() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    fig_w = 140 * MM
    fig_h = 120 * MM
    fx = (A4_W_PT - fig_w) / 2
    fy = (A4_H_PT - fig_h) / 2
    fig_rect = fitz.Rect(fx, fy, fx + fig_w, fy + fig_h)
    page.draw_rect(fig_rect, color=(0, 0, 0), width=0.8)

    # Build a small triangle mesh by subdividing a quad grid with alternating
    # diagonals.  Coordinates are local; we translate by (fx, fy) at draw time.
    nx = 12
    ny = 8
    pitch_x = (fig_w - 20 * MM) / nx
    pitch_y = (fig_h - 20 * MM) / ny
    origin_x = fx + 10 * MM
    origin_y = fy + 10 * MM

    nodes = [
        (origin_x + i * pitch_x, origin_y + j * pitch_y)
        for j in range(ny + 1)
        for i in range(nx + 1)
    ]

    def node_idx(i: int, j: int) -> int:
        return j * (nx + 1) + i

    # Quad edges.
    for j in range(ny + 1):
        for i in range(nx):
            page.draw_line(nodes[node_idx(i, j)], nodes[node_idx(i + 1, j)], color=(0, 0, 0), width=0.25)
    for j in range(ny):
        for i in range(nx + 1):
            page.draw_line(nodes[node_idx(i, j)], nodes[node_idx(i, j + 1)], color=(0, 0, 0), width=0.25)
    # Diagonals alternating per cell.
    for j in range(ny):
        for i in range(nx):
            if (i + j) % 2 == 0:
                page.draw_line(nodes[node_idx(i, j)], nodes[node_idx(i + 1, j + 1)], color=(0, 0, 0), width=0.25)
            else:
                page.draw_line(nodes[node_idx(i + 1, j)], nodes[node_idx(i, j + 1)], color=(0, 0, 0), width=0.25)

    # Coordinate axes in the bottom-left.
    ax_x0 = origin_x - 4 * MM
    ax_y0 = origin_y - 4 * MM
    ax_x1 = origin_x + nx * pitch_x + 8 * MM
    ax_y1 = origin_y + ny * pitch_y + 8 * MM
    page.draw_line((ax_x0, ax_y0), (ax_x1, ax_y0), color=(0, 0, 0), width=0.8)
    page.draw_line((ax_x0, ax_y0), (ax_x0, ax_y1), color=(0, 0, 0), width=0.8)
    # Tick marks.
    for i in range(nx + 1):
        x = origin_x + i * pitch_x
        page.draw_line((x, ax_y0 - 1 * MM), (x, ax_y0 + 1 * MM), color=(0, 0, 0), width=0.5)
    for j in range(ny + 1):
        y = origin_y + j * pitch_y
        page.draw_line((ax_x0 - 1 * MM, y), (ax_x0 + 1 * MM, y), color=(0, 0, 0), width=0.5)

    # An arrow somewhere in the mesh (force indicator).
    arr_x = origin_x + 4 * pitch_x
    arr_y = origin_y + 3 * pitch_y
    page.draw_line((arr_x, arr_y), (arr_x + 18 * MM, arr_y + 6 * MM), color=(0.8, 0, 0), width=1.0)
    page.insert_text((arr_x + 18 * MM, arr_y + 6 * MM), "F", fontsize=12, color=(0.8, 0, 0))

    # Node labels (sparse).
    page.insert_text((origin_x, origin_y - 6 * MM), "Node 1", fontsize=9, color=(0, 0, 0))
    page.insert_text(
        (origin_x + nx * pitch_x - 10 * MM, origin_y - 6 * MM),
        "Node 2",
        fontsize=9,
        color=(0, 0, 0),
    )

    # Title above figure.
    page.insert_text(
        (fx + 6 * MM, fy + fig_h - 4 * MM),
        "Figure: Finite element mesh",
        fontsize=11,
        color=(0, 0, 0),
    )

    out = FIXTURES / "case3_fem_mesh.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# Case 4 -- two figures, 25 mm gap.
def make_case4_wide_gap() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    fig_w = 80 * MM
    fig_h = 80 * MM
    gap = 25 * MM
    total_w = 2 * fig_w + gap
    start_x = (A4_W_PT - total_w) / 2
    y = (A4_H_PT - fig_h) / 2

    for i in range(2):
        x0 = start_x + i * (fig_w + gap)
        page.draw_rect(fitz.Rect(x0, y, x0 + fig_w, y + fig_h), color=(0, 0, 0), width=0.8)
        # Some content.
        page.insert_text(
            (x0 + 5 * MM, y + fig_h - 6 * MM),
            f"Figure {i + 1}",
            fontsize=11,
            color=(0, 0, 0),
        )
        # Diagonal line for visual interest.
        page.draw_line(
            (x0 + 5 * MM, y + 5 * MM),
            (x0 + fig_w - 5 * MM, y + fig_h - 5 * MM),
            color=(0.2, 0.2, 0.2),
            width=0.6,
        )

    out = FIXTURES / "case4_wide_gap.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# Case 5 -- two figures, 3 mm gap.
def make_case5_close_gap() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    fig_w = 90 * MM
    fig_h = 90 * MM
    gap = 3 * MM
    total_w = 2 * fig_w + gap
    start_x = (A4_W_PT - total_w) / 2
    y = (A4_H_PT - fig_h) / 2

    for i in range(2):
        x0 = start_x + i * (fig_w + gap)
        page.draw_rect(fitz.Rect(x0, y, x0 + fig_w, y + fig_h), color=(0, 0, 0), width=0.8)
        page.insert_text(
            (x0 + 5 * MM, y + fig_h - 6 * MM),
            f"Figure {i + 1}",
            fontsize=11,
            color=(0, 0, 0),
        )
        # Some interior lines.
        for k in range(4):
            ly = y + (10 + k * 14) * MM
            page.draw_line(
                (x0 + 5 * MM, ly),
                (x0 + fig_w - 5 * MM, ly),
                color=(0.4, 0.4, 0.4),
                width=0.5,
            )

    out = FIXTURES / "case5_close_gap.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# Case 6 -- multi-page document.
def make_case6_multipage() -> Path:
    doc = _new_blank_a4()
    # Page 1 -- 1 figure
    page = doc[0]
    fw, fh = 130 * MM, 130 * MM
    fx = (A4_W_PT - fw) / 2
    fy = (A4_H_PT - fh) / 2
    page.draw_rect(fitz.Rect(fx, fy, fx + fw, fy + fh), color=(0, 0, 0), width=0.8)
    page.insert_text((fx + 6 * MM, fy + fh - 6 * MM), "Page 1 / Figure 1", fontsize=11)
    page.draw_line((fx + 5 * MM, fy + 5 * MM), (fx + fw - 5 * MM, fy + fh - 5 * MM), color=(0.2, 0.2, 0.2), width=0.5)

    # Page 2 -- 4 figures (2x2, each its own frame)
    doc.insert_page(1, width=A4_W_PT, height=A4_H_PT)
    page = doc[1]
    cell_w = 70 * MM
    cell_h = 80 * MM
    h_gap = 20 * MM
    v_gap = 30 * MM
    total_w = 2 * cell_w + h_gap
    total_h = 2 * cell_h + v_gap
    start_x = (A4_W_PT - total_w) / 2
    start_y = (A4_H_PT - total_h) / 2
    for r in range(2):
        for c in range(2):
            cx0 = start_x + c * (cell_w + h_gap)
            cx1 = cx0 + cell_w
            if r == 0:
                rect_y0 = start_y + v_gap + cell_h
                rect_y1 = start_y + total_h
            else:
                rect_y0 = start_y
                rect_y1 = start_y + cell_h
            page.draw_rect(
                fitz.Rect(cx0, rect_y0, cx1, rect_y1),
                color=(0, 0, 0),
                width=0.6,
            )
            page.insert_text(
                (cx0 + 4 * MM, rect_y1 - 4 * MM),
                f"P2 ({r + 1},{c + 1})",
                fontsize=10,
            )

    # Page 3 -- 2 figures, side by side
    doc.insert_page(2, width=A4_W_PT, height=A4_H_PT)
    page = doc[2]
    fw, fh = 80 * MM, 100 * MM
    gap = 25 * MM
    total = 2 * fw + gap
    sx = (A4_W_PT - total) / 2
    sy = (A4_H_PT - fh) / 2
    for i in range(2):
        x0 = sx + i * (fw + gap)
        page.draw_rect(fitz.Rect(x0, sy, x0 + fw, sy + fh), color=(0, 0, 0), width=0.8)
        page.insert_text((x0 + 4 * MM, sy + fh - 6 * MM), f"P3 Fig {i + 1}", fontsize=11)

    out = FIXTURES / "case6_multipage.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# Case 7 -- vector-only single figure (for preservation verification).
def make_case7_vector_only() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    fig_w = 120 * MM
    fig_h = 90 * MM
    fx = (A4_W_PT - fig_w) / 2
    fy = (A4_H_PT - fig_h) / 2
    page.draw_rect(fitz.Rect(fx, fy, fx + fig_w, fy + fig_h), color=(0, 0, 0), width=0.8)

    # Lots of very thin lines + tiny text.
    cx, cy = fx + fig_w / 2, fy + fig_h / 2
    for i in range(36):
        theta = i * (2 * math.pi / 36)
        x2 = cx + (40 * MM) * math.cos(theta)
        y2 = cy + (30 * MM) * math.sin(theta)
        page.draw_line((cx, cy), (x2, y2), color=(0, 0, 0), width=0.3)

    # Sparse text labels.
    labels = [
        ("A", fx + 10 * MM, fy + 12 * MM),
        ("B", fx + fig_w - 15 * MM, fy + 12 * MM),
        ("C", fx + fig_w / 2, fy + fig_h - 10 * MM),
    ]
    for txt, tx, ty in labels:
        page.insert_text((tx, ty), txt, fontsize=10, color=(0, 0, 0))

    page.insert_text(
        (fx + 6 * MM, fy + fig_h - 6 * MM),
        "Vector-only figure (thin lines + text).",
        fontsize=10,
        color=(0, 0, 0),
    )

    out = FIXTURES / "case7_vector_only.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# Driver.

# ===========================================================================
# Diagnostics -- 8 hand-crafted cases to probe algorithm weak spots.
# ===========================================================================


def _draw_simple_figure(
    page: fitz.Page,
    x0_pt: float,
    y0_pt: float,
    w_pt: float,
    h_pt: float,
    label: str,
    *,
    style: str = "lines",
) -> None:
    """Draw a bordered figure with synthetic interior content.

    ``y0_pt`` is the **bottom** edge of the figure in PDF coordinates (PyMuPDF
    origin is bottom-left).  ``w_pt``/``h_pt`` are width/height in points.
    The figure is bordered with a thin black rectangle and filled with one of
    several deterministic content patterns so each figure is visually distinct.
    """
    rect = fitz.Rect(x0_pt, y0_pt, x0_pt + w_pt, y0_pt + h_pt)
    page.draw_rect(rect, color=(0, 0, 0), width=0.6)
    # Label sits near the bottom-left of the figure in PDF coords
    # (i.e. near the bottom in the rendered view).
    page.insert_text(
        (x0_pt + 3 * MM, y0_pt + 4 * MM),
        label,
        fontsize=8,
        color=(0, 0, 0),
    )

    if style == "lines":
        # Horizontal lines evenly spaced inside the figure.
        n = max(2, int(h_pt / (12 * MM)))
        for i in range(n):
            ly = y0_pt + (10 + i * 10) * MM
            page.draw_line(
                (x0_pt + 4 * MM, ly),
                (x0_pt + w_pt - 4 * MM, ly),
                color=(0.4, 0.4, 0.4),
                width=0.4,
            )
    elif style == "curves":
        # Stylised curve drawn as short line segments forming a sine wave.
        cx = x0_pt + w_pt / 2
        n_segments = 24
        prev = None
        for i in range(n_segments + 1):
            t = i / n_segments
            x = x0_pt + 5 * MM + t * (w_pt - 10 * MM)
            y = y0_pt + h_pt / 2 + (h_pt / 4) * math.sin(t * 4 * math.pi)
            if prev is not None:
                page.draw_line(prev, (x, y), color=(0.3, 0.3, 0.3), width=0.4)
            prev = (x, y)
    elif style == "grid":
        # Uniform 2D grid of thin lines.
        nx = max(2, int(w_pt / (10 * MM)))
        ny = max(2, int(h_pt / (10 * MM)))
        for i in range(nx + 1):
            x = x0_pt + 5 * MM + i * (w_pt - 10 * MM) / nx
            page.draw_line(
                (x, y0_pt + 5 * MM),
                (x, y0_pt + h_pt - 5 * MM),
                color=(0.5, 0.5, 0.5),
                width=0.3,
            )
        for j in range(ny + 1):
            y = y0_pt + 5 * MM + j * (h_pt - 10 * MM) / ny
            page.draw_line(
                (x0_pt + 5 * MM, y),
                (x0_pt + w_pt - 5 * MM, y),
                color=(0.5, 0.5, 0.5),
                width=0.3,
            )
    elif style == "scatter":
        # Deterministic-looking scatter dots.
        rng = random.Random(hash(label) & 0xFFFF)
        for _ in range(20):
            px = x0_pt + 5 * MM + rng.random() * (w_pt - 10 * MM)
            py = y0_pt + 5 * MM + rng.random() * (h_pt - 10 * MM)
            page.draw_circle(
                (px, py), 0.6 * MM, color=(0, 0, 0.7), width=0,
                fill=(0, 0, 0.7),
            )
    elif style == "diagonal":
        # A few diagonal lines from corners.
        page.draw_line(
            (x0_pt + 5 * MM, y0_pt + 5 * MM),
            (x0_pt + w_pt - 5 * MM, y0_pt + h_pt - 5 * MM),
            color=(0.2, 0.2, 0.2),
            width=0.5,
        )
        page.draw_line(
            (x0_pt + w_pt - 5 * MM, y0_pt + 5 * MM),
            (x0_pt + 5 * MM, y0_pt + h_pt - 5 * MM),
            color=(0.2, 0.2, 0.2),
            width=0.5,
        )


# ---------------------------------------------------------------------------
# D1: 1x3 horizontal row, 15mm gap.  Expected: 3 figures.
def make_d1_horizontal_3() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    fig_w = 50 * MM
    fig_h = 80 * MM
    gap = 15 * MM
    total_w = 3 * fig_w + 2 * gap  # 180 mm
    start_x = (A4_W_PT - total_w) / 2
    y0 = (A4_H_PT - fig_h) / 2

    for i, style in enumerate(["lines", "curves", "grid"]):
        x0 = start_x + i * (fig_w + gap)
        _draw_simple_figure(page, x0, y0, fig_w, fig_h, f"D1.F{i + 1}", style=style)

    out = FIXTURES_DIAG / "D1_horizontal_3.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# D2: 3+1 -- top row 3 small, bottom row 1 big.  Expected: 4 figures.
def make_d2_3plus1() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    # Layout (top-down mm):
    #   row 1: 3 small figs, each 40x50 mm, 15 mm gaps
    #   gap: 30 mm
    #   row 2: 1 big fig, 140x100 mm
    top_w_mm, top_h_mm, top_gap_mm = 40, 50, 15
    bot_w_mm, bot_h_mm = 140, 100
    v_gap_mm = 30

    # Horizontal positions
    top_total_w_mm = 3 * top_w_mm + 2 * top_gap_mm  # 150 mm
    top_start_x_mm = (210.0 - top_total_w_mm) / 2   # 30 mm
    bot_x_mm = (210.0 - bot_w_mm) / 2                # 35 mm

    # Vertical positions (mm from top of page, top-down view)
    total_h_mm = top_h_mm + v_gap_mm + bot_h_mm      # 180 mm
    top_y_top_mm = (297.0 - total_h_mm) / 2          # 58.5 mm from top
    top_y_bot_mm = top_y_top_mm + top_h_mm           # 108.5 mm from top
    bot_y_top_mm = top_y_bot_mm + v_gap_mm           # 138.5 mm from top
    bot_y_bot_mm = bot_y_top_mm + bot_h_mm           # 238.5 mm from top

    def mm_to_pdf_y(y_mm_from_top: float) -> float:
        return (297.0 - y_mm_from_top) * MM

    # Top row (3 small figs)
    top_y0_pt = mm_to_pdf_y(top_y_bot_mm)
    top_w_pt = top_w_mm * MM
    top_h_pt = top_h_mm * MM
    for i, style in enumerate(["lines", "curves", "grid"]):
        x0_pt = (top_start_x_mm + i * (top_w_mm + top_gap_mm)) * MM
        _draw_simple_figure(page, x0_pt, top_y0_pt, top_w_pt, top_h_pt,
                            f"D2.T{i + 1}", style=style)

    # Bottom row (1 big fig)
    bot_y0_pt = mm_to_pdf_y(bot_y_bot_mm)
    _draw_simple_figure(page, bot_x_mm * MM, bot_y0_pt,
                        bot_w_mm * MM, bot_h_mm * MM,
                        "D2.B1", style="diagonal")

    out = FIXTURES_DIAG / "D2_3plus1.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# D3: 1 big left + 2x2 grid right.  Expected: 5 figures.
def make_d3_big_left_4right() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    big_w_mm, big_h_mm = 80, 130
    sm_w_mm, sm_h_mm = 45, 60
    sm_gap_x_mm, sm_gap_y_mm = 10, 10
    h_gap_mm = 15

    grid_w_mm = 2 * sm_w_mm + sm_gap_x_mm  # 100 mm
    grid_h_mm = 2 * sm_h_mm + sm_gap_y_mm  # 130 mm
    total_w_mm = big_w_mm + h_gap_mm + grid_w_mm  # 195 mm
    start_x_mm = (210.0 - total_w_mm) / 2            # 7.5 mm

    # Big left fig: centred vertically
    big_y_top_mm = (297.0 - big_h_mm) / 2
    big_y_bot_mm = big_y_top_mm + big_h_mm

    def mm_to_pdf_y(y_mm_from_top: float) -> float:
        return (297.0 - y_mm_from_top) * MM

    big_y0_pt = mm_to_pdf_y(big_y_bot_mm)
    _draw_simple_figure(page, start_x_mm * MM, big_y0_pt,
                        big_w_mm * MM, big_h_mm * MM,
                        "D3.L1", style="diagonal")

    # 2x2 grid right -- top-down positions in mm
    grid_y_top_mm = (297.0 - grid_h_mm) / 2
    grid_y_bot_mm = grid_y_top_mm + grid_h_mm
    right_x_mm = start_x_mm + big_w_mm + h_gap_mm
    positions = [
        (0, 0, grid_y_top_mm, sm_h_mm, "lines"),     # upper-left
        (1, 0, grid_y_top_mm, sm_h_mm, "curves"),    # upper-right
        (0, 1, grid_y_top_mm + sm_h_mm + sm_gap_y_mm, sm_h_mm, "grid"),
        (1, 1, grid_y_top_mm + sm_h_mm + sm_gap_y_mm, sm_h_mm, "scatter"),
    ]
    for col, row, y_top_mm, h_mm, style in positions:
        x0_pt = (right_x_mm + col * (sm_w_mm + sm_gap_x_mm)) * MM
        y0_pt = mm_to_pdf_y(y_top_mm + h_mm)
        _draw_simple_figure(page, x0_pt, y0_pt,
                            sm_w_mm * MM, h_mm * MM,
                            f"D3.R{row + 1}{col + 1}", style=style)

    out = FIXTURES_DIAG / "D3_big_left_4right.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# D4: dense FEM-style mesh + axes + text + large blank interior (30x30mm).
# Expected: 1 figure (the mesh perimeter should keep it connected).
def make_d4_fem_blank_interior() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    fig_w_mm, fig_h_mm = 140, 130
    fig_x_mm = (210.0 - fig_w_mm) / 2
    fig_y_top_mm = (297.0 - fig_h_mm) / 2
    fig_y_bot_mm = fig_y_top_mm + fig_h_mm

    def mm_to_pdf_y(y_mm_from_top: float) -> float:
        return (297.0 - y_mm_from_top) * MM

    fx = fig_x_mm * MM
    fy_top_pt = mm_to_pdf_y(fig_y_top_mm)
    fy_bot_pt = mm_to_pdf_y(fig_y_bot_mm)

    # Outer border
    page.draw_rect(
        fitz.Rect(fx, fy_bot_pt, fx + fig_w_mm * MM, fy_top_pt),
        color=(0, 0, 0),
        width=0.8,
    )

    # Triangle mesh with pitch ~2.5 mm inside the figure.  A 30x30 mm
    # blank area is punched out in the middle of the mesh.
    pitch = 2.5 * MM
    nx = int(fig_w_mm / 2.5) - 4
    ny = int(fig_h_mm / 2.5) - 4
    fig_w_pt = fig_w_mm * MM
    fig_h_pt = fig_h_mm * MM
    origin_x = fx + 2 * MM
    origin_y = fy_bot_pt + 2 * MM  # bottom-left of grid
    pitch_x = (fig_w_pt - 4 * MM) / nx
    pitch_y = (fig_h_pt - 4 * MM) / ny

    def idx(i: int, j: int) -> Tuple[float, float]:
        return (origin_x + i * pitch_x, origin_y + j * pitch_y)

    # Blank centre range
    blank_w_pt = 30 * MM
    blank_h_pt = 30 * MM
    cx_fig = fx + fig_w_pt / 2
    cy_fig = (fy_top_pt + fy_bot_pt) / 2
    blank_x_lo = cx_fig - blank_w_pt / 2
    blank_x_hi = cx_fig + blank_w_pt / 2
    blank_y_lo = cy_fig - blank_h_pt / 2
    blank_y_hi = cy_fig + blank_h_pt / 2

    def in_blank(pt: Tuple[float, float]) -> bool:
        return (blank_x_lo <= pt[0] <= blank_x_hi
                and blank_y_lo <= pt[1] <= blank_y_hi)

    # Horizontal edges
    for j in range(ny + 1):
        for i in range(nx):
            p1 = idx(i, j)
            p2 = idx(i + 1, j)
            if in_blank(p1) and in_blank(p2):
                continue
            page.draw_line(p1, p2, color=(0, 0, 0), width=0.2)
    # Vertical edges
    for j in range(ny):
        for i in range(nx + 1):
            p1 = idx(i, j)
            p2 = idx(i, j + 1)
            if in_blank(p1) and in_blank(p2):
                continue
            page.draw_line(p1, p2, color=(0, 0, 0), width=0.2)
    # Alternating diagonals (sketch a triangle mesh)
    for j in range(ny):
        for i in range(nx):
            if (i + j) % 2 == 0:
                p1 = idx(i, j)
                p2 = idx(i + 1, j + 1)
            else:
                p1 = idx(i + 1, j)
                p2 = idx(i, j + 1)
            if in_blank(p1) and in_blank(p2):
                continue
            page.draw_line(p1, p2, color=(0, 0, 0), width=0.2)

    # Coordinate axes at the bottom-left of the mesh region.
    ax_x0 = origin_x - 3 * MM
    ax_y0 = origin_y - 3 * MM
    ax_x1 = origin_x + nx * pitch_x + 4 * MM
    ax_y1 = origin_y + ny * pitch_y + 4 * MM
    page.draw_line((ax_x0, ax_y0), (ax_x1, ax_y0), color=(0, 0, 0), width=0.6)
    page.draw_line((ax_x0, ax_y0), (ax_x0, ax_y1), color=(0, 0, 0), width=0.6)

    # Force arrow inside the figure (outside the blank area)
    arr_x = origin_x + 10 * pitch_x
    arr_y = origin_y + 8 * pitch_y
    page.draw_line(
        (arr_x, arr_y), (arr_x + 15 * MM, arr_y + 5 * MM),
        color=(0.8, 0, 0), width=1.0,
    )
    page.insert_text((arr_x + 15 * MM, arr_y + 5 * MM), "F",
                     fontsize=10, color=(0.8, 0, 0))

    # Title above figure (visually = higher y in PDF)
    page.insert_text(
        (fx + 4 * MM, fy_top_pt + 3 * MM),
        "Figure: FEM mesh with internal blank region",
        fontsize=10,
        color=(0, 0, 0),
    )

    out = FIXTURES_DIAG / "D4_fem_blank_interior.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# D5: topology optimisation -- many small filled polygons forming an
# organic-looking structure.  Expected: 1 figure.
def make_d5_topology() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    fig_w_mm, fig_h_mm = 140, 130
    fig_x_mm = (210.0 - fig_w_mm) / 2
    fig_y_top_mm = (297.0 - fig_h_mm) / 2
    fig_y_bot_mm = fig_y_top_mm + fig_h_mm

    def mm_to_pdf_y(y_mm_from_top: float) -> float:
        return (297.0 - y_mm_from_top) * MM

    fx = fig_x_mm * MM
    fy_top_pt = mm_to_pdf_y(fig_y_top_mm)
    fy_bot_pt = mm_to_pdf_y(fig_y_bot_mm)
    fig_rect = fitz.Rect(fx, fy_bot_pt, fx + fig_w_mm * MM, fy_top_pt)
    page.draw_rect(fig_rect, color=(0, 0, 0), width=0.8)

    # Build an organic topology pattern: roughly elliptical envelope filled
    # with small irregular hexagons.  The hexagons are densely packed and
    # share boundaries so they form a single connected blob.
    rng = random.Random(42)
    cx = fx + fig_w_mm * MM / 2
    cy = (fy_top_pt + fy_bot_pt) / 2
    a = fig_w_mm * MM / 2 - 6 * MM
    b = fig_h_mm * MM / 2 - 6 * MM
    hex_r = 2.5 * MM  # hexagon radius (centre to vertex)

    hexagons: list = []
    step = hex_r * math.sqrt(3)  # vertical spacing
    rows = int(2 * b / (step * 0.75)) + 4
    for row in range(-rows, rows + 1):
        offset = (step / 2) if row % 2 != 0 else 0
        y = cy + row * (step * 0.75)
        if abs(y - cy) > b + hex_r:
            continue
        ratio = 1.0 - ((y - cy) ** 2) / (b ** 2)
        if ratio < 0:
            continue
        x_extent = a * math.sqrt(ratio)
        nx = int(x_extent / (hex_r * 1.5)) + 1
        for col in range(-nx, nx + 1):
            hx = cx + col * (hex_r * 1.5) + offset
            if abs(hx - cx) > x_extent:
                continue
            jx = hx + (rng.random() - 0.5) * 0.3 * MM
            jy = y + (rng.random() - 0.5) * 0.3 * MM
            hexagons.append((jx, jy))

    # Draw each hexagon as 6 line segments forming a closed contour.
    for (hx, hy) in hexagons:
        pts = []
        for k in range(6):
            theta = math.pi / 6 + k * (math.pi / 3)
            pts.append((hx + hex_r * math.cos(theta), hy + hex_r * math.sin(theta)))
        for k in range(6):
            page.draw_line(pts[k], pts[(k + 1) % 6], color=(0, 0, 0), width=0.25)

    # Title above figure (visually = higher y in PDF)
    page.insert_text(
        (fx + 4 * MM, fy_top_pt + 3 * MM),
        "Figure: Topology optimisation result",
        fontsize=10,
        color=(0, 0, 0),
    )

    out = FIXTURES_DIAG / "D5_topology.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# D6: 2 figures with 0.5 mm gap -- below every preset's merge threshold.
# Expected: 2 figures; algorithm will likely merge into 1.
def make_d6_tiny_gap() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    fig_w = 80 * MM
    fig_h = 100 * MM
    gap = 0.5 * MM  # ~5 px at 250 DPI; < all preset merge_gaps
    total_w = 2 * fig_w + gap  # 160.5 mm
    start_x = (A4_W_PT - total_w) / 2
    y0 = (A4_H_PT - fig_h) / 2

    for i in range(2):
        x0 = start_x + i * (fig_w + gap)
        _draw_simple_figure(page, x0, y0, fig_w, fig_h, f"D6.F{i + 1}",
                            style=["lines", "curves"][i])

    out = FIXTURES_DIAG / "D6_tiny_gap.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# D7: 3 figures vertically; top-middle gap 20 mm, middle-bottom gap 1 mm.
# Expected: 3 figures; algorithm will likely merge the lower pair.
def make_d7_mixed_gaps() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    fig_w = 80 * MM
    fig_h = 60 * MM
    gap_top = 20 * MM
    gap_bot = 1 * MM
    total_h = 3 * fig_h + gap_top + gap_bot  # 201 mm
    y_top_pdf = (A4_H_PT + total_h) / 2  # top edge of top figure (PDF)
    y0_top = y_top_pdf - fig_h

    x = (A4_W_PT - fig_w) / 2
    styles = ["lines", "curves", "grid"]
    for i in range(3):
        if i == 0:
            y0 = y0_top
        elif i == 1:
            y0 = y0_top - fig_h - gap_top
        else:  # i == 2
            y0 = y0_top - 2 * fig_h - gap_top - gap_bot
        _draw_simple_figure(page, x, y0, fig_w, fig_h, f"D7.F{i + 1}", style=styles[i])

    out = FIXTURES_DIAG / "D7_mixed_gaps.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# D8: 1 row of 3 figures with widths 30, 60, 30 mm.  Expected: 3 figures.
def make_d8_mixed_widths() -> Path:
    doc = _new_blank_a4()
    page = doc[0]

    widths = [30 * MM, 60 * MM, 30 * MM]
    fig_h = 70 * MM
    gap = 15 * MM
    total_w = sum(widths) + 2 * gap  # 150 mm
    start_x = (A4_W_PT - total_w) / 2
    y0 = (A4_H_PT - fig_h) / 2

    x = start_x
    for i, w in enumerate(widths):
        _draw_simple_figure(page, x, y0, w, fig_h, f"D8.F{i + 1}",
                            style=["lines", "curves", "grid"][i])
        x += w + gap

    out = FIXTURES_DIAG / "D8_mixed_widths.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ---------------------------------------------------------------------------
# Driver.

def main() -> None:
    cases = [
        make_case1_single_figure,
        make_case2_four_figures,
        make_case3_fem_mesh,
        make_case4_wide_gap,
        make_case5_close_gap,
        make_case6_multipage,
        make_case7_vector_only,
    ]
    print(f"== Original fixtures -> {FIXTURES} ==")
    for fn in cases:
        path = fn()
        # Quick sanity-check: re-open and verify at least one page exists.
        verify = fitz.open(str(path))
        page_count = len(verify)
        verify.close()
        size_kb = path.stat().st_size / 1024.0
        print(f"  {path.name:40s} pages={page_count} size={size_kb:7.1f} KB")

    diag_cases = [
        ("D1", "1x3 horizontal (15mm gap)", 3, make_d1_horizontal_3),
        ("D2", "3+1 asymmetric", 4, make_d2_3plus1),
        ("D3", "1 big left + 2x2 grid right", 5, make_d3_big_left_4right),
        ("D4", "dense FEM mesh with blank interior", 1, make_d4_fem_blank_interior),
        ("D5", "topology optimisation", 1, make_d5_topology),
        ("D6", "2 figures, 0.5mm gap (sub-threshold)", 2, make_d6_tiny_gap),
        ("D7", "3 figures, mixed gaps (20mm + 1mm)", 3, make_d7_mixed_gaps),
        ("D8", "1 row 3 figures (30,60,30 mm wide)", 3, make_d8_mixed_widths),
    ]
    print(f"\n== Diagnostic fixtures -> {FIXTURES_DIAG} ==")
    for code, name, expected, fn in diag_cases:
        path = fn()
        verify = fitz.open(str(path))
        page_count = len(verify)
        verify.close()
        size_kb = path.stat().st_size / 1024.0
        print(f"  {path.name:40s} pages={page_count} size={size_kb:7.1f} KB "
              f"(expected={expected} fig: {name})")
    print(f"\nAll {len(cases)} original + {len(diag_cases)} diagnostic fixtures written.")


if __name__ == "__main__":
    main()