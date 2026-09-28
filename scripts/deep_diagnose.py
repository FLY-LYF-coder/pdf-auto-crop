"""Deep diagnosis of failing cases.

For each case where standard detection disagrees with the expected count,
walk through every stage of the pipeline and dump enough detail to pinpoint
where things go wrong:

* raw mask: count of foreground pixels and bbox of all dark content
* closed mask: count of foreground pixels after morph_close
* connected components on closed mask: bbox + area of every blob
* merged components: bbox + area of every blob after merge_close_boxes
* survivors after filter_components

Mask statistics and the closed mask are also saved as PNGs for manual
inspection (under ``diag_deep/<case>/``).

The point is to answer: was the failure caused by
  (a) morph_close kernel too small (figure fragmented into multiple blobs),
  (b) merge_close_boxes too eager (multiple blobs merged into one), or
  (c) something else (filter / refine / wrong initial threshold)?
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import cv2
import numpy as np
import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pdf_auto_crop.config import CropConfig  # noqa: E402
from pdf_auto_crop.image_processor import (  # noqa: E402
    binarize,
    compute_adaptive_params,
    filter_components,
    find_components,
    merge_close_boxes,
    morph_close,
    refine_bbox,
)
from pdf_auto_crop.pdf_processor import render_page_to_array  # noqa: E402


FIXTURES = ROOT / "tests" / "fixtures_diag"
DEEP_DIR = ROOT / "diag_deep"
DEEP_DIR.mkdir(exist_ok=True)


@dataclass(frozen=True)
class CaseSpec:
    code: str
    pdf_path: Path
    expected: int
    description: str


CASES: List[CaseSpec] = [
    CaseSpec("D1", FIXTURES / "D1_horizontal_3.pdf", 3, "1x3 horizontal row, 15mm gap"),
    CaseSpec("D2", FIXTURES / "D2_3plus1.pdf", 4, "3 small + 1 big"),
    CaseSpec("D3", FIXTURES / "D3_big_left_4right.pdf", 5, "1 big + 2x2 grid"),
    CaseSpec("D4", FIXTURES / "D4_fem_blank_interior.pdf", 1, "FEM mesh + blank interior"),
    CaseSpec("D5", FIXTURES / "D5_topology.pdf", 1, "topology optimisation"),
    CaseSpec("D6", FIXTURES / "D6_tiny_gap.pdf", 2, "0.5mm gap (sub-threshold)"),
    CaseSpec("D7", FIXTURES / "D7_mixed_gaps.pdf", 3, "20mm + 1mm gaps"),
    CaseSpec("D8", FIXTURES / "D8_mixed_widths.pdf", 3, "mixed widths 30/60/30"),
]


PRESETS = ("standard", "tight", "loose")


def _stats_for_mask(mask: np.ndarray) -> Dict[str, float]:
    """Compute basic statistics on a binary mask."""
    total = mask.size
    fg = int((mask > 0).sum())
    return {
        "total_px": int(total),
        "fg_px": fg,
        "fg_ratio": fg / total if total else 0.0,
    }


def _diagnose_case(case: CaseSpec, preset_name: str = "standard") -> Dict[str, object]:
    """Run the full pipeline step by step on ``case.pdf`` with the named preset.

    Returns a dict with stats for every stage so the caller can pretty-print.
    """
    cfg = CropConfig()  # base config
    if preset_name != "standard":
        from pdf_auto_crop.presets import apply_preset
        cfg = apply_preset(cfg, preset_name)

    doc = fitz.open(str(case.pdf_path))
    try:
        rgb, (page_w_pt, page_h_pt) = render_page_to_array(doc, 0, cfg.dpi)
    finally:
        doc.close()

    h, w = rgb.shape[:2]
    raw_mask = binarize(rgb, cfg.threshold)
    raw_stats = _stats_for_mask(raw_mask)
    raw_components = find_components(raw_mask)

    params = compute_adaptive_params(cfg.dpi, cfg)
    closed_mask = morph_close(raw_mask, params["close_kernel"])
    closed_stats = _stats_for_mask(closed_mask)
    closed_components = find_components(closed_mask)

    merged = merge_close_boxes(closed_components, params["merge_gap_px"])
    survivors = filter_components(merged, params)

    refined: List[Tuple[int, int, int, int]] = []
    for bbox in survivors:
        refined.append(refine_bbox(raw_mask, bbox))

    return {
        "case": case,
        "preset": preset_name,
        "page_w_pt": page_w_pt,
        "page_h_pt": page_h_pt,
        "rgb_shape": (h, w),
        "threshold": cfg.threshold,
        "params": params,
        "raw_stats": raw_stats,
        "raw_component_count": len(raw_components),
        "closed_stats": closed_stats,
        "closed_component_count": len(closed_components),
        "merged_count": len(merged),
        "survivor_count": len(survivors),
        "refined_count": len(refined),
        "closed_components": closed_components,
        "merged": merged,
        "survivors": survivors,
        "refined": refined,
        "raw_mask": raw_mask,
        "closed_mask": closed_mask,
    }


def _save_closed_mask_png(case_dir: Path, preset_name: str, closed_mask: np.ndarray) -> None:
    """Save the closed mask as a PNG for visual inspection."""
    out = (closed_mask > 0).astype(np.uint8) * 255
    cv2.imwrite(str(case_dir / f"closed_{preset_name}.png"), out)


def _bbox_str(bbox_px: Tuple[int, int, int, int], dpi: int) -> str:
    x0, y0, w, h = bbox_px
    x1, y1 = x0 + w, y0 + h
    scale = 25.4 / dpi
    return (
        f"({x0 * scale:5.1f},{y0 * scale:5.1f})-({x1 * scale:5.1f},{y1 * scale:5.1f}) "
        f"{w * scale:5.1f}x{h * scale:5.1f}mm"
    )


def main() -> int:
    print("=" * 96)
    print("Deep diagnosis -- pipeline stage-by-stage on every case")
    print("=" * 96)
    print()

    summary: List[Tuple[str, int, int, str]] = []

    for case in CASES:
        if not case.pdf_path.exists():
            continue

        case_dir = DEEP_DIR / case.code
        case_dir.mkdir(exist_ok=True)

        print(f"=== {case.code}: {case.description} (expected={case.expected}) ===")
        info = _diagnose_case(case, "standard")
        _save_closed_mask_png(case_dir, "standard", info["closed_mask"])

        params = info["params"]
        page_w_mm = info["page_w_pt"] * 25.4 / 72
        page_h_mm = info["page_h_pt"] * 25.4 / 72
        print(
            f"  page: {page_w_mm:.0f}x{page_h_mm:.0f}mm  threshold={info['threshold']}  "
            f"close_kernel={params['close_kernel']}  merge_gap_px={params['merge_gap_px']} "
            f"min_area_px={params['min_area_px']}"
        )
        print(
            f"  raw mask:        fg_px={info['raw_stats']['fg_px']:>7d} "
            f"({info['raw_stats']['fg_ratio'] * 100:5.2f}%)  "
            f"components={info['raw_component_count']}"
        )
        print(
            f"  closed mask:     fg_px={info['closed_stats']['fg_px']:>7d} "
            f"({info['closed_stats']['fg_ratio'] * 100:5.2f}%)  "
            f"components={info['closed_component_count']}"
        )
        print(f"  after merge:     blobs={info['merged_count']}")
        print(f"  after filter:    blobs={info['survivor_count']}")
        print(f"  after refine:    blobs={info['refined_count']}")

        dpi = int(info["params"]["close_kernel"] * 25 / max(1, info["params"]["close_kernel"])) or 250
        dpi = 250  # standard cfg.dpi used for rendering
        print("  merged bboxes (mm, top-left origin):")
        for i, bbox in enumerate(info["merged"]):
            print(f"    [{i}] {_bbox_str(bbox, dpi)}")

        verdict = "PASS" if info["refined_count"] == case.expected else "FAIL"
        diff = info["refined_count"] - case.expected
        sign = "+" if diff > 0 else ""
        print(
            f"  verdict: {verdict}  (detected={info['refined_count']}, "
            f"diff={sign}{diff})"
        )
        print()
        summary.append((case.code, case.expected, info["refined_count"], verdict))

    print("=" * 96)
    print("SUMMARY (standard preset)")
    print("=" * 96)
    for code, exp, det, verdict in summary:
        sign = "+" if det - exp > 0 else ""
        print(f"  {code}: expected={exp}, detected={det}, diff={sign}{det - exp} -> {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())