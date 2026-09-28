"""Compare legacy vs smart detection on every fixture.

Prints a side-by-side table so the effect of the new algorithm is visible at a
glance, including the merge kernel and threshold that were selected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pdf_auto_crop.config import CropConfig  # noqa: E402
from pdf_auto_crop.detector import detect_figures  # noqa: E402
from pdf_auto_crop.pdf_processor import render_page_to_array  # noqa: E402
from pdf_auto_crop.smart_detector import smart_detect_figures  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
DIAG = ROOT / "tests" / "fixtures_diag"

CASES = [
    ("case1", FIX / "case1_single_figure.pdf", 1),
    ("case2", FIX / "case2_four_figures.pdf", 4),
    ("case3", FIX / "case3_fem_mesh.pdf", 1),
    ("case4", FIX / "case4_wide_gap.pdf", 2),
    ("case5", FIX / "case5_close_gap.pdf", 2),
    ("case6", FIX / "case6_multipage.pdf", 7),
    ("case7", FIX / "case7_vector_only.pdf", 1),
    ("D1", DIAG / "D1_horizontal_3.pdf", 3),
    ("D2", DIAG / "D2_3plus1.pdf", 4),
    ("D3", DIAG / "D3_big_left_4right.pdf", 5),
    ("D4", DIAG / "D4_fem_blank_interior.pdf", 1),
    ("D5", DIAG / "D5_topology.pdf", 1),
    ("D6", DIAG / "D6_tiny_gap.pdf", 2),
    ("D7", DIAG / "D7_mixed_gaps.pdf", 3),
    ("D8", DIAG / "D8_mixed_widths.pdf", 3),
]


def main() -> int:
    cfg = CropConfig()
    print(f"{'Case':6s} {'Exp':>4s} {'legacy':>8s} {'smart':>7s}  {'verdict':8s} per-page detail")
    print("-" * 96)
    total_ok = 0
    total = 0
    for code, path, expected in CASES:
        doc = fitz.open(str(path))
        legacy_counts: list[int] = []
        smart_counts: list[int] = []
        try:
            for page_index in range(len(doc)):
                rgb, size = render_page_to_array(doc, page_index, cfg.dpi)
                legacy_counts.append(len(detect_figures(rgb, size, cfg)))
                smart_counts.append(len(smart_detect_figures(rgb, size, cfg)))
        finally:
            doc.close()

        legacy = sum(legacy_counts)
        smart = sum(smart_counts)
        ok = smart == expected
        total_ok += 1 if ok else 0
        total += 1
        verdict = "PASS" if ok else "FAIL"
        detail = f"legacy pages={legacy_counts} smart pages={smart_counts}"
        print(f"{code:6s} {expected:>4d} {legacy:>8d} {smart:>7d}  {verdict:8s} {detail}")
    print("-" * 96)
    print(f"smart: {total_ok}/{total} cases match expected count")
    return 0 if total_ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
