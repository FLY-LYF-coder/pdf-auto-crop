"""Explore real inter-component gap distributions for every fixture.

This is a *measurement* script used while designing :mod:`smart_detector`.
It prints, per PDF and per candidate close-kernel, the number of connected
components and the distinct inter-component gaps (in mm) with their pair
counts, so the gap-threshold rule can be grounded in real data rather than
guesswork.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

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
)

MM_PER_PX = 25.4 / 250.0


@dataclass(frozen=True)
class Spec:
    code: str
    path: Path
    expected: int


def _all_specs() -> list[Spec]:
    fx = ROOT / "tests" / "fixtures"
    fd = ROOT / "tests" / "fixtures_diag"
    return [
        Spec("case1", fx / "case1_single_figure.pdf", 1),
        Spec("case2", fx / "case2_four_figures.pdf", 4),
        Spec("case3", fx / "case3_fem_mesh.pdf", 1),
        Spec("case4", fx / "case4_wide_gap.pdf", 2),
        Spec("case5", fx / "case5_close_gap.pdf", 2),
        Spec("case6", fx / "case6_multipage.pdf", 7),
        Spec("case7", fx / "case7_vector_only.pdf", 1),
        Spec("D1", fd / "D1_horizontal_3.pdf", 3),
        Spec("D2", fd / "D2_3plus1.pdf", 4),
        Spec("D3", fd / "D3_big_left_4right.pdf", 5),
        Spec("D4", fd / "D4_fem_blank_interior.pdf", 1),
        Spec("D5", fd / "D5_topology.pdf", 1),
        Spec("D6", fd / "D6_tiny_gap.pdf", 2),
        Spec("D7", fd / "D7_mixed_gaps.pdf", 3),
        Spec("D8", fd / "D8_mixed_widths.pdf", 3),
    ]


def _gaps(comps):
    """Reference implementation of the inter-component gap sweep."""
    out = []
    n = len(comps)
    for i in range(n):
        xi, yi, wi, hi = comps[i]
        for j in range(i + 1, n):
            xj, yj, wj, hj = comps[j]
            ri = (xi, yi, xi + wi, yi + hi)
            rj = (xj, yj, xj + wj, yj + hj)
            if min(ri[3], rj[3]) > max(ri[1], rj[1]):
                if ri[2] <= rj[0]:
                    out.append(rj[0] - ri[2])
                elif rj[2] <= ri[0]:
                    out.append(ri[0] - rj[2])
            if min(ri[2], rj[2]) > max(ri[0], rj[0]):
                if ri[3] <= rj[1]:
                    out.append(rj[1] - ri[3])
                elif rj[3] <= ri[1]:
                    out.append(ri[1] - rj[3])
    return out


def _format_gaps(gaps: list[int]) -> str:
    if not gaps:
        return "(no gaps)"
    c = Counter(gaps)
    parts = [f"{g * MM_PER_PX:.2f}mm x{n}" for g, n in sorted(c.items())]
    return ", ".join(parts)


def _count_for(comps, threshold_px, params):
    merged = merge_close_boxes(comps, threshold_px)
    survivors = filter_components(merged, params)
    return len(survivors)


def main() -> int:
    cfg = CropConfig()
    params = compute_adaptive_params(cfg.dpi, cfg)
    kernels = [3, 5, 7, 9, 11, 15]

    print("=" * 100)
    print("Gap exploration -- per-kernel component counts and gap structure")
    print("=" * 100)
    print(f"default merge_gap_px = {params['merge_gap_px']} "
          f"({params['merge_gap_px'] * MM_PER_PX:.2f} mm)")
    print()

    for spec in _all_specs():
        doc = fitz.open(str(spec.path))
        try:
            print(f"### {spec.code}  (expected {spec.expected})  {spec.path.name}")
            for page_index in range(len(doc)):
                page = doc[page_index]
                pix = page.get_pixmap(dpi=cfg.dpi)
                rgb = __import__("numpy").frombuffer(pix.samples, dtype="uint8").reshape(
                    pix.height, pix.width, pix.n
                )[:, :, :3].copy()
                raw = binarize(rgb, cfg.threshold)
                print(f"  -- page {page_index + 1} --")
                for k in kernels:
                    closed = morph_close(raw, k)
                    comps = find_components(closed)
                    gaps = _gaps(comps)
                    if not comps:
                        print(f"     k={k:>2}: 0 components")
                        continue
                    desc = _format_gaps(gaps)
                    if len(desc) > 150:
                        desc = desc[:147] + "..."
                    print(f"     k={k:>2}: {len(comps):>3} comps | {desc}")
        finally:
            doc.close()
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
