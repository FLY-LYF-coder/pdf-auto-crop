"""Render detection previews for a few fixtures so the boxes can be eyeballed.

Counting figures correctly is not enough -- a detector can hit the right
number while placing the boxes in the wrong place.  These PNGs draw the
detected rectangles on top of the rendered page for visual confirmation.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pymupdf as fitz  # noqa: E402

from pdf_auto_crop.config import CropConfig  # noqa: E402
from pdf_auto_crop.pdf_processor import render_page_to_array  # noqa: E402
from pdf_auto_crop.preview import draw_preview, save_preview  # noqa: E402
from pdf_auto_crop.smart_detector import smart_detect_figures  # noqa: E402

OUT = ROOT / "diag_out" / "previews"
OUT.mkdir(parents=True, exist_ok=True)

TARGETS = [
    ("D7_mixed_gaps", ROOT / "tests" / "fixtures_diag" / "D7_mixed_gaps.pdf"),
    ("D2_3plus1", ROOT / "tests" / "fixtures_diag" / "D2_3plus1.pdf"),
    ("D3_big_left_4right", ROOT / "tests" / "fixtures_diag" / "D3_big_left_4right.pdf"),
    ("D6_tiny_gap", ROOT / "tests" / "fixtures_diag" / "D6_tiny_gap.pdf"),
    ("case2_four_figures", ROOT / "tests" / "fixtures" / "case2_four_figures.pdf"),
    ("D4_fem_blank_interior", ROOT / "tests" / "fixtures_diag" / "D4_fem_blank_interior.pdf"),
]


def main() -> int:
    cfg = CropConfig()
    for name, path in TARGETS:
        doc = fitz.open(str(path))
        try:
            for page_index in range(len(doc)):
                rgb, size = render_page_to_array(doc, page_index, cfg.dpi)
                rects = smart_detect_figures(rgb, size, cfg)
                img = draw_preview(
                    rgb, rects, dpi=cfg.dpi,
                    labels=[f"F{i + 1}" for i in range(len(rects))],
                )
                out = OUT / f"{name}_smart.png"
                save_preview(img, out)
                boxes = "; ".join(
                    f"({r.x0 * 25.4 / 72:.0f},{r.y0 * 25.4 / 72:.0f})-"
                    f"({r.x1 * 25.4 / 72:.0f},{r.y1 * 25.4 / 72:.0f})mm"
                    for r in rects
                )
                print(f"{name} p{page_index + 1}: {len(rects)} figure(s) -> {out}")
                print(f"    {boxes}")
        finally:
            doc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
