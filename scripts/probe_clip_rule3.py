"""Probe: the final clip rule, verified over the whole matrix.

Rule under test:

* an unrotated page: the clip is the region's rect in the page's normalised
  top-down frame;
* a rotated page: the same rect, shifted by the CropBox's own origin
  (x as stored, y negated).

The CropBox is read from the page dictionary rather than from
``Page.cropbox``, because PyMuPDF reports that property with its y
coordinates flipped.

Run:  python scripts/probe_clip_rule3.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from pdf_auto_crop import compositor as comp  # noqa: E402
from test_compositor import _render, _red_bbox  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "diag_out" / "clip_rule"
OUT.mkdir(parents=True, exist_ok=True)


def stored_cropbox(doc: fitz.Document, pno: int) -> fitz.Rect:
    """The CropBox exactly as the file stores it.

    An absent CropBox means "the same as the MediaBox".
    """
    page = doc[pno]
    raw = doc.xref_get_key(page.xref, "CropBox")[1]
    if raw == "null":
        return fitz.Rect(page.mediabox)
    nums = [float(v) for v in raw.strip("[] ").split()]
    return fitz.Rect(*nums)


def build(path: Path, rotation: int, origin: tuple[int, int],
          inset: int) -> None:
    w, h = 400, 600
    ox, oy = origin
    doc = fitz.open()
    page = doc.new_page(width=w, height=h)
    if origin != (0, 0):
        page.set_mediabox(fitz.Rect(ox, oy, ox + w, oy + h))
    if inset:
        # A CropBox that stays inside the MediaBox.
        cb = fitz.Rect(ox + inset, oy + inset, ox + w - inset, oy + h - inset)
        page.set_cropbox(fitz.Rect(cb.x0, (oy + h) - cb.y1,
                                    cb.x1, (oy + h) - cb.y0))
    page.draw_rect(fitz.Rect(60, 90, 300, 230), color=(0.85, 0.1, 0.1),
                   fill=(0.85, 0.1, 0.1), width=0)
    page.draw_rect(fitz.Rect(70, 100, 110, 140), color=(0.1, 0.1, 0.85),
                   fill=(0.1, 0.1, 0.85), width=0)
    page.set_rotation(rotation)
    doc.save(str(path))
    doc.close()


def bake(src: Path, clip: fitz.Rect, rotation: int,
         size: tuple[float, float]) -> np.ndarray:
    out = fitz.open()
    p = out.new_page(width=size[0], height=size[1])
    p.show_pdf_page(fitz.Rect(0, 0, size[0], size[1]), fitz.open(str(src)), 0,
                    rotate=comp.embed_rotation(rotation), clip=clip)
    path = OUT / "tmp.pdf"
    out.save(str(path))
    out.close()
    return _render(path, dpi=72)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cliprule3_"))
    ok_all = True
    for origin in ((0, 0), (20, 50), (35, 17), (0, 80), (-25, 40), (100, 30)):
        for inset in (0, 25, 60):
            for rotation in (0, 90, 180, 270):
                src = OUT / f"r_{origin[0]}_{origin[1]}_{inset}_{rotation}.pdf"
                build(src, rotation, origin, inset)

                doc = fitz.open(str(src))
                page = doc[0]
                cb = stored_cropbox(doc, 0)
                visible = fitz.Rect(page.cropbox)
                doc.close()

                vw, vh = visible.width, visible.height
                region = fitz.Rect(60, vh - 230, 300, vh - 90)
                n = comp.region_to_page_rect(region, fitz.Rect(0, 0, vw, vh))
                if rotation in (0,):
                    clip = n
                else:
                    clip = fitz.Rect(n.x0 + cb.x0, n.y0 - cb.y0,
                                     n.x1 + cb.x0, n.y1 - cb.y0)

                rw, rh = region.width, region.height
                size = ((rh, rw) if rotation in (90, 270) else (rw, rh))

                full = _render(src, dpi=72)
                box = _red_bbox(full)
                if box is None:
                    print(f"origin {origin} inset {inset} rot {rotation}: "
                          f"no red found")
                    ok_all = False
                    continue
                x0, y0, x1, y1 = box
                expected = full[y0:y1, x0:x1]
                got = bake(src, clip, rotation, size)
                h = min(expected.shape[0], got.shape[0])
                w = min(expected.shape[1], got.shape[1])
                d = np.abs(expected[:h, :w] - got[:h, :w]).max(axis=2)
                changed = int((d > 24).sum())
                if changed:
                    ok_all = False
                    print(f"origin {origin} inset {inset} rot {rotation}: "
                          f"diff {changed}  cropbox {tuple(cb)} "
                          f"clip {tuple(clip)}")
    print(f"\n=> {'the rule holds in every case' if ok_all
          else 'the rule does NOT hold everywhere'}")


if __name__ == "__main__":
    main()
