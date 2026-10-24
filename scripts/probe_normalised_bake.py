"""Probe: a bake that does not depend on the source page's boxes at all.

The trouble with ``show_pdf_page(clip=...)`` is that MuPDF reads the clip in
a frame that depends on the page's own MediaBox and CropBox -- and, for a
page that carries a ``/Rotate``, in the *rotated* frame.  The rule that
relates the two was found empirically, but it does not hold for every
combination of boxes.

This probe tries a construction that sidesteps the whole question:

1. copy the source page into a scratch document and take its ``/Rotate``
   off.  The page's visible frame is untouched, but the page is now
   unrotated -- so the clip is read in the ordinary, unrotated frame and the
   rule is the simple one;
2. lift the region out of that copy with the clip;
3. turn the result by the page's rotation, about a known origin.

The pivot of the turn does not matter: what is wanted is the region's own
shape, and a turn is a turn.

Run:  python scripts/probe_normalised_bake.py
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

OUT = Path(__file__).resolve().parent.parent / "diag_out" / "normalised_bake"
OUT.mkdir(parents=True, exist_ok=True)


def build(path: Path, rotation: int, origin: tuple[int, int],
          inset: int) -> None:
    w, h = 400, 600
    ox, oy = origin
    doc = fitz.open()
    page = doc.new_page(width=w, height=h)
    if origin != (0, 0):
        page.set_mediabox(fitz.Rect(ox, oy, ox + w, oy + h))
    if inset:
        cb = fitz.Rect(ox + inset, oy + inset, ox + w - inset, oy + h - inset)
        page.set_cropbox(fitz.Rect(cb.x0, (oy + h) - cb.y1,
                                    cb.x1, (oy + h) - cb.y0))
    page.draw_rect(fitz.Rect(80, 100, 320, 240), color=(0.85, 0.1, 0.1),
                   fill=(0.85, 0.1, 0.1), width=0)
    page.draw_rect(fitz.Rect(90, 110, 130, 150), color=(0.1, 0.1, 0.85),
                   fill=(0.1, 0.1, 0.85), width=0)
    page.set_rotation(rotation)
    doc.save(str(path))
    doc.close()


def normalised_bake(src: Path, region: fitz.Rect, visible: fitz.Rect,
                    rotation: int) -> np.ndarray:
    """The three-stage bake described above."""
    rw, rh = region.width, region.height

    # 1. copy the page and take the rotation off
    scratch = fitz.open()
    scratch.insert_pdf(fitz.open(str(src)))
    scratch[0].set_rotation(0)

    # 2. lift the region out
    clip = comp.region_to_page_rect(region, visible)
    stage1 = fitz.open()
    p1 = stage1.new_page(width=rw, height=rh)
    p1.show_pdf_page(fitz.Rect(0, 0, rw, rh), scratch, 0, rotate=0, clip=clip)
    stage1_path = OUT / "stage1.pdf"
    stage1.save(str(stage1_path))
    stage1.close()
    scratch.close()

    # 3. turn it
    w, h = comp.displayed_size(rw, rh, rotation)
    stage2 = fitz.open()
    p2 = stage2.new_page(width=w, height=h)
    p2.show_pdf_page(fitz.Rect(0, 0, w, h), fitz.open(str(stage1_path)), 0,
                     rotate=comp.embed_rotation(rotation),
                     clip=fitz.Rect(0, 0, rw, rh))
    out_path = OUT / "stage2.pdf"
    stage2.save(str(out_path))
    stage2.close()
    return _render(out_path, dpi=72)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="normbake_"))
    ok_all = True
    for origin in ((0, 0), (20, 50), (35, 17), (0, 80), (-25, 40), (100, 30)):
        for inset in (0, 25, 60):
            for rotation in (0, 90, 180, 270):
                src = OUT / f"n_{origin[0]}_{origin[1]}_{inset}_{rotation}.pdf"
                build(src, rotation, origin, inset)

                doc = fitz.open(str(src))
                page = doc[0]
                visible = fitz.Rect(page.cropbox)
                doc.close()

                vw, vh = visible.width, visible.height
                region = fitz.Rect(80, vh - 240, 320, vh - 100)

                full = _render(src, dpi=72)
                box = _red_bbox(full)
                if box is None:
                    print(f"origin {origin} inset {inset} rot {rotation}: "
                          f"no red found")
                    ok_all = False
                    continue
                x0, y0, x1, y1 = box
                expected = full[y0:y1, x0:x1]
                got = normalised_bake(src, region, visible, rotation)
                h = min(expected.shape[0], got.shape[0])
                w = min(expected.shape[1], got.shape[1])
                d = np.abs(expected[:h, :w] - got[:h, :w]).max(axis=2)
                changed = int((d > 24).sum())
                if changed:
                    ok_all = False
                    print(f"origin {origin} inset {inset} rot {rotation}: "
                          f"diff {changed}  visible {tuple(visible)}")
    print(f"\n=> {'the normalised bake reproduces the viewer in every case'
          if ok_all else 'the normalised bake still differs somewhere'}")


if __name__ == "__main__":
    main()
