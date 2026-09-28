"""Diagnose pdf-auto-crop detection on 8 hand-crafted diagnostic PDFs.

For every case the script:
  1. Renders the page at the configured DPI.
  2. Runs :func:`detect_figures` once per preset (standard/tight/loose).
  3. Prints the count and the bbox of each detected figure.
  4. Compares against an expected count and emits PASS / FAIL.

The CLI is bypassed on purpose -- we want every bbox in mm to compare
against expectations rather than only the count emitted by the pipeline.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pymupdf as fitz

# Ensure the in-tree package is importable when running from elsewhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pdf_auto_crop.config import CropConfig  # noqa: E402
from pdf_auto_crop.detector import detect_figures  # noqa: E402
from pdf_auto_crop.pdf_processor import render_page_to_array  # noqa: E402
from pdf_auto_crop.presets import apply_preset  # noqa: E402


FIXTURES = ROOT / "tests" / "fixtures_diag"
DIAG_OUT = ROOT / "diag_out"
DIAG_OUT.mkdir(exist_ok=True)


@dataclass(frozen=True)
class CaseSpec:
    """Spec for one diagnostic case."""
    code: str
    pdf_path: Path
    expected: int
    description: str


CASES: List[CaseSpec] = [
    CaseSpec("D1", FIXTURES / "D1_horizontal_3.pdf", 3,
             "1x3 horizontal row, 15mm gap"),
    CaseSpec("D2", FIXTURES / "D2_3plus1.pdf", 4,
             "3 small figs top + 1 big fig bottom"),
    CaseSpec("D3", FIXTURES / "D3_big_left_4right.pdf", 5,
             "1 big left + 2x2 grid right"),
    CaseSpec("D4", FIXTURES / "D4_fem_blank_interior.pdf", 1,
             "dense FEM mesh with 30x30mm blank interior"),
    CaseSpec("D5", FIXTURES / "D5_topology.pdf", 1,
             "topology optimisation (hexagonal blob)"),
    CaseSpec("D6", FIXTURES / "D6_tiny_gap.pdf", 2,
             "2 figures with 0.5mm gap (sub-threshold)"),
    CaseSpec("D7", FIXTURES / "D7_mixed_gaps.pdf", 3,
             "3 vertical figures, 20mm + 1mm gaps"),
    CaseSpec("D8", FIXTURES / "D8_mixed_widths.pdf", 3,
             "1 row, 3 figs of widths 30,60,30mm"),
]


PRESETS = ("standard", "tight", "loose")


def _format_bbox_mm(rect: fitz.Rect, page_h_pt: float) -> str:
    """Format a PDF bbox as (x0,y0,x1,y1) in mm with origin at top-left."""
    # PyMuPDF: PDF y0 is bottom of rect, y1 is top.  Convert to mm-from-top.
    x0_mm = rect.x0 * 25.4 / 72
    y1_mm = (rect.y1) * 25.4 / 72         # bottom-edge of rect in PDF y
    y0_mm_from_top = page_h_pt - rect.y1   # top-down y
    y1_mm_from_top = page_h_pt - rect.y0   # bottom-down y (larger)
    # Easier: use the page height in mm and PyMuPDF y values in mm.
    page_h_mm = page_h_pt * 25.4 / 72
    top_down_y0 = page_h_mm - rect.y1 * 25.4 / 72  # mm from top
    top_down_y1 = page_h_mm - rect.y0 * 25.4 / 72
    w_mm = rect.width * 25.4 / 72
    h_mm = rect.height * 25.4 / 72
    return (
        f"({x0_mm:5.1f},{top_down_y0:5.1f})-({x0_mm + w_mm:5.1f},{top_down_y1:5.1f})"
        f"  {w_mm:5.1f}x{h_mm:5.1f}mm"
    )


def _detect(case: CaseSpec, preset: str) -> tuple[int, list[fitz.Rect], float]:
    """Run detection with ``preset`` on ``case.pdf``.  Returns (count, rects, page_h_pt)."""
    cfg = apply_preset(CropConfig(), preset)
    doc = fitz.open(str(case.pdf_path))
    try:
        rgb, (page_w_pt, page_h_pt) = render_page_to_array(doc, 0, cfg.dpi)
        rects = detect_figures(rgb, (page_w_pt, page_h_pt), cfg)
        return len(rects), rects, page_h_pt
    finally:
        doc.close()


def _verdict(case: CaseSpec, counts: dict) -> tuple[str, str]:
    """Return (PASS/FAIL, reason)."""
    std = counts["standard"]
    expected = case.expected
    extras = []
    for preset_name, count in counts.items():
        diff = count - expected
        sign = "+" if diff > 0 else ""
        extras.append(f"{preset_name}={count}({sign}{diff})")
    if std == expected:
        return "PASS", ", ".join(extras)
    return "FAIL", ", ".join(extras) + f"  expected={expected}"


def _bbox_summary(rects: list, page_h_pt: float) -> str:
    if not rects:
        return "(none)"
    return "; ".join(_format_bbox_mm(r, page_h_pt) for r in rects)


def main() -> int:
    print("=" * 88)
    print("pdf-auto-crop diagnostic run -- 8 hand-crafted cases, 3 presets")
    print("=" * 88)
    print(f"Fixtures dir: {FIXTURES}")
    print(f"Output dir:   {DIAG_OUT}")
    print()

    summary: list[tuple[str, int, int, int, int, str]] = []

    for case in CASES:
        if not case.pdf_path.exists():
            print(f"!!! Missing fixture: {case.pdf_path}")
            continue

        counts: dict[str, int] = {}
        rects_by_preset: dict[str, list[fitz.Rect]] = {}
        page_h_pt: float = 0.0
        for preset in PRESETS:
            n, rects, page_h_pt = _detect(case, preset)
            counts[preset] = n
            rects_by_preset[preset] = rects

        verdict, reason = _verdict(case, counts)
        print(f"=== {case.code}: {case.description} ===")
        print(f"  Expected: {case.expected} figures")
        for preset in PRESETS:
            n = counts[preset]
            rects = rects_by_preset[preset]
            print(f"  Detected ({preset:>8s}): {n} figure(s) -> "
                  f"{_bbox_summary(rects, page_h_pt)}")
        print(f"  Verdict: {verdict}  ({reason})")
        print()

        summary.append((
            case.code,
            case.expected,
            counts["standard"],
            counts["tight"],
            counts["loose"],
            verdict,
        ))

    # Summary table
    print("=" * 88)
    print("SUMMARY")
    print("=" * 88)
    print(f"  {'Case':4s} {'Expected':>9s} {'Standard':>9s} {'Tight':>9s} {'Loose':>9s}  Verdict")
    print("  " + "-" * 64)
    for code, exp, std, tight, loose, verdict in summary:
        print(f"  {code:4s} {exp:>9d} {std:>9d} {tight:>9d} {loose:>9d}  {verdict}")
    pass_count = sum(1 for s in summary if s[5] == "PASS")
    print(f"\n  Pass: {pass_count}/{len(summary)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())