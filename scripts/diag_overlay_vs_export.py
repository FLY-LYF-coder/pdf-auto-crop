"""Measure exactly how the preview overlay differs from the exported CropBox.

Reproduces the GUI's own code path end to end: render -> detect -> what the
canvas would draw -> what ``run_pipeline`` would actually write -> what the
written PDF's CropBox really is.  Prints the discrepancy in millimetres so the
cause is a number rather than a guess.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

import pymupdf as fitz  # noqa: E402

from pdf_auto_crop.config import CropConfig  # noqa: E402
from pdf_auto_crop.geometry import add_margin_pt, mm_to_pt  # noqa: E402
from pdf_auto_crop.gui import build_pipeline_args  # noqa: E402
from pdf_auto_crop.pdf_processor import render_page_to_array  # noqa: E402
from pdf_auto_crop.pipeline import get_detector, run_pipeline  # noqa: E402

FIXTURES = [
    ROOT / "tests" / "fixtures_diag" / "D7_mixed_gaps.pdf",
    ROOT / "tests" / "fixtures_diag" / "D3_big_left_4right.pdf",
    ROOT / "tests" / "fixtures" / "case2_four_figures.pdf",
]

MM = 25.4 / 72.0


def report(label: str, drawn: fitz.Rect, written: fitz.Rect) -> None:
    dw, dh = (drawn.x1 - drawn.x0) * MM, (drawn.y1 - drawn.y0) * MM
    ww, wh = (written.x1 - written.x0) * MM, (written.y1 - written.y0) * MM
    shift_x = (written.x0 - drawn.x0) * MM
    shift_y = (written.y0 - drawn.y0) * MM
    verdict = "OK  " if (abs(shift_x) < 0.05 and abs(shift_y) < 0.05
                         and abs(ww - dw) < 0.05) else "DIFF"
    print(f"    [{verdict}] {label:<12} drawn {dw:7.1f}x{dh:6.1f}mm @({drawn.x0*MM:6.1f},{drawn.y0*MM:6.1f})"
          f"  written {ww:7.1f}x{wh:6.1f}mm @({written.x0*MM:6.1f},{written.y0*MM:6.1f})"
          f"  d=({shift_x:+.2f},{shift_y:+.2f})mm")


def main() -> int:
    cfg = CropConfig()
    print(f"config: margin_mm={cfg.margin_mm}  padding_mm={cfg.padding_mm}  "
          f"dpi={cfg.dpi}  naming={cfg.naming}  algorithm={cfg.algorithm}")
    print(f"=> pipeline expands every DETECTED rect by "
          f"{cfg.margin_mm + cfg.padding_mm:.1f} mm on each side\n")

    for src in FIXTURES:
        print(f"{src.name}")
        doc = fitz.open(str(src))
        try:
            for pi in range(len(doc)):
                rgb, size_pt = render_page_to_array(doc, pi, cfg.dpi)
                rects = get_detector(cfg)(rgb, size_pt, cfg)
                page_rect = fitz.Rect(0, 0, size_pt[0], size_pt[1])
                margin_pt = mm_to_pt(cfg.margin_mm + cfg.padding_mm)
                for i, r in enumerate(rects[:2], start=1):
                    report(f"auto fig {i}", r, add_margin_pt(r, margin_pt, page_rect))
        finally:
            doc.close()
        print()

    # What the written file actually contains, versus both predictions.
    print("written-file check (D7):")
    src = FIXTURES[0]
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "o"
        run_pipeline(build_pipeline_args(src, out), cfg)
        doc = fitz.open(str(src))
        try:
            rgb, size_pt = render_page_to_array(doc, 0, cfg.dpi)
            rects = get_detector(cfg)(rgb, size_pt, cfg)
        finally:
            doc.close()
        for i, r in enumerate(rects, start=1):
            f = out / f"figure_p01_f{i:02d}.pdf"
            d = fitz.open(str(f))
            try:
                report(f"fig {i}", r, d[0].cropbox)
            finally:
                d.close()

    # Manual path: the rectangle the user drew, verbatim?
    print("\nmanual path (hand-drawn rect, margin must NOT be re-applied):")
    drawn = fitz.Rect(100, 200, 300, 500)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "m"
        run_pipeline(build_pipeline_args(src, out), cfg, manual_boxes={0: [drawn]})
        d = fitz.open(str(out / "figure_p01_f01.pdf"))
        try:
            report("manual", drawn, d[0].cropbox)
        finally:
            d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
