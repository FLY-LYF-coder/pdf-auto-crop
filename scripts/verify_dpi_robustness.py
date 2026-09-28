"""Verify detection is DPI-adaptive rather than tuned to one resolution.

Every pixel-level constant in the smart detector is derived from the render
DPI, so the same fixtures must resolve identically at 150, 250, 400 and 600
DPI.  This script re-runs all 15 fixtures across that sweep and reports the
first failing case per DPI.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pymupdf as fitz  # noqa: E402

from pdf_auto_crop.config import CropConfig  # noqa: E402
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

DPI_SWEEP = (150, 250, 400, 600)


def _count_at_dpi(path: Path, cfg: CropConfig) -> int:
    doc = fitz.open(str(path))
    try:
        total = 0
        for page_index in range(len(doc)):
            rgb, size = render_page_to_array(doc, page_index, cfg.dpi)
            total += len(smart_detect_figures(rgb, size, cfg))
        return total
    finally:
        doc.close()


def main() -> int:
    failures = 0
    for dpi in DPI_SWEEP:
        cfg = CropConfig(dpi=dpi)
        passed = 0
        broken: list[str] = []
        for code, path, expected in CASES:
            got = _count_at_dpi(path, cfg)
            if got == expected:
                passed += 1
            else:
                broken.append(f"{code}:{got}!={expected}")
        failures += len(broken)
        detail = "all pass" if not broken else " ".join(broken)
        print(f"dpi={dpi:>3}: {passed}/{len(CASES)}  {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
