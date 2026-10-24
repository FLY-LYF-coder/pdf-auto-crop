"""Probe: pin down the bake's geometry for rotated pages and offset pages.

Findings so far (see scripts/probe_bake_rotate_square.py):

* The production bake is wrong for rotated pages on a non-square page.
  ``_displayed_offset`` mixed the region's user-space coordinates with the
  visible rect's page-space coordinates.
* The existing differential test cannot see this: its source page is square
  and its region is centred, so every rotation maps the region onto itself.
* The source page's ``/Rotate`` is clockwise, but ``show_pdf_page(rotate=)``
  turns the content the *other* way, so the two must be compensated.

This probe checks the corrected formulas against the pixel oracle for

* four rotations,
* a non-square region,
* a page whose MediaBox does not start at the origin,

and compares two bake strategies: trimming with a CropBox, and clipping the
content with ``show_pdf_page(clip=...)``.

Run:  python scripts/probe_bake_fix2.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pymupdf as fitz

OUT = Path(__file__).resolve().parent.parent / "diag_out" / "bake_fix2"

# Page geometry: 400x600, optionally offset.
PAGE_W, PAGE_H = 400, 600

# A non-square region, off-centre, in *page* (top-down) coordinates.
REGION_TD = fitz.Rect(60, 90, 260, 290)          # 200 x 200 -- square
REGION_TD = fitz.Rect(60, 90, 300, 230)          # 240 x 140 -- not square


def build_source(path: Path, rotation: int, offset: tuple[int, int]) -> None:
    ox, oy = offset
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    # draw_rect works in page space; shift it with the mediabox origin.
    r = fitz.Rect(REGION_TD.x0 + ox, REGION_TD.y0 + oy,
                  REGION_TD.x1 + ox, REGION_TD.y1 + oy)
    page.draw_rect(r, color=(0.85, 0.1, 0.1), fill=(0.85, 0.1, 0.1), width=0)
    m = fitz.Rect(r.x0 + 10, r.y0 + 10, r.x0 + 50, r.y0 + 50)
    page.draw_rect(m, color=(0.1, 0.1, 0.85), fill=(0.1, 0.1, 0.85), width=0)
    page.set_rotation(rotation)
    doc.save(str(path))
    doc.close()


def render(doc: fitz.Document, dpi: int = 72) -> np.ndarray:
    pix = doc[0].get_pixmap(dpi=dpi)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    if pix.n == 1:
        arr = np.repeat(arr, 3, axis=2)
    return arr[:, :, :3].astype(int)


def marker_corner(arr: np.ndarray) -> str:
    blue = (arr[:, :, 2] > 120) & (arr[:, :, 0] < 120)
    ys, xs = np.where(blue)
    if len(xs) == 0:
        return "none"
    cx, cy = xs.mean() / arr.shape[1], ys.mean() / arr.shape[0]
    return f"{'left' if cx < 0.5 else 'right'}-{'top' if cy < 0.5 else 'bottom'}"


def red_bbox(arr: np.ndarray):
    red = (arr[:, :, 0] > 150) & (arr[:, :, 1] < 120)
    ys, xs = np.where(red)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def footprint(region, vw: float, vh: float, rotation: int):
    """Where the region lands in the displayed frame, relative to the
    displayed visible rect's top-left corner.

    ``region`` is in user space relative to the visible rect's bottom-left.
    """
    x0, y0, x1, y1 = region
    if rotation == 90:
        return (y0, x0, y1, x1)
    if rotation == 180:
        return (vw - x1, y0, vw - x0, y1)
    if rotation == 270:
        return (vh - y1, vw - x1, vh - y0, vw - x0)
    return (x0, vh - y1, x1, vh - y0)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    all_ok = True

    for offset in ((0, 0), (20, 50)):
        print(f"\n######## MediaBox origin {offset} ########")
        for rotation in (0, 90, 180, 270):
            src_path = OUT / f"src_{offset[0]}_{offset[1]}_{rotation}.pdf"
            build_source(src_path, rotation, offset)

            doc = fitz.open(str(src_path))
            page = doc[0]
            visible = fitz.Rect(page.cropbox) & fitz.Rect(page.mediabox)
            full = render(doc)
            doc.close()

            bb = red_bbox(full)
            if bb is None:
                print(f"rotation {rotation}: no red box found")
                continue
            bx0, by0, bx1, by1 = bb
            expected = full[by0:by1, bx0:bx1]
            expect_corner = marker_corner(expected)

            # The region in the project's user space: relative to the visible
            # rect's bottom-left, y up.
            region = fitz.Rect(
                REGION_TD.x0 + offset[0] - visible.x0,
                visible.y1 - (REGION_TD.y1 + offset[1]),
                REGION_TD.x1 + offset[0] - visible.x0,
                visible.y1 - (REGION_TD.y0 + offset[1]))

            vw, vh = visible.width, visible.height
            fx0, fy0, fx1, fy1 = footprint(region, vw, vh, rotation)
            dw, dh = (vh, vw) if rotation in (90, 270) else (vw, vh)

            # The region in the source page's absolute page space.
            clip = fitz.Rect(visible.x0 + region.x0, visible.y1 - region.y1,
                             visible.x0 + region.x1, visible.y1 - region.y0)
            rw, rh = region.width, region.height
            tw, th = (rh, rw) if rotation in (90, 270) else (rw, rh)

            results = []

            # A': cropbox trim, rotation compensated (360 - r).
            out = fitz.open()
            p = out.new_page(width=dw, height=dh)
            p.show_pdf_page(fitz.Rect(0, 0, dw, dh), fitz.open(str(src_path)),
                            0, rotate=(360 - rotation) % 360)
            p.set_cropbox(fitz.Rect(fx0, fy0, fx1, fy1))
            ap = OUT / f"a_{offset[0]}_{offset[1]}_{rotation}.pdf"
            out.save(str(ap))
            out.close()
            got = render(fitz.open(str(ap)))
            h = min(expected.shape[0], got.shape[0])
            w = min(expected.shape[1], got.shape[1])
            d = np.abs(expected[:h, :w] - got[:h, :w]).max(axis=2)
            results.append(("A' cropbox", int((d > 24).sum()),
                            marker_corner(got)))

            # B': clip based, rotation compensated.
            out = fitz.open()
            p = out.new_page(width=tw, height=th)
            p.show_pdf_page(fitz.Rect(0, 0, tw, th), fitz.open(str(src_path)),
                            0, rotate=(360 - rotation) % 360, clip=clip)
            bp = OUT / f"b_{offset[0]}_{offset[1]}_{rotation}.pdf"
            out.save(str(bp))
            out.close()
            got = render(fitz.open(str(bp)))
            h = min(expected.shape[0], got.shape[0])
            w = min(expected.shape[1], got.shape[1])
            d = np.abs(expected[:h, :w] - got[:h, :w]).max(axis=2)
            results.append(("B' clip   ", int((d > 24).sum()),
                            marker_corner(got)))

            print(f"\n=== rotation {rotation} "
                  f"(visible {vw:.0f}x{vh:.0f}, region {rw:.0f}x{rh:.0f}) ===")
            print(f"  oracle: box at ({bx0},{by0})-({bx1},{by1}) "
                  f"{expected.shape[1]}x{expected.shape[0]}px, "
                  f"marker {expect_corner}")
            print(f"  footprint {fx0:.0f},{fy0:.0f} -> {fx1:.0f},{fy1:.0f}")
            for label, changed, corner in results:
                ok = changed == 0
                all_ok = all_ok and ok
                print(f"  {label}: diff {changed:6d}  marker {corner:12s}"
                      f"{'  OK' if ok else '  MISMATCH'}")

    print(f"\n=> {'ALL VARIANTS MATCH THE ORACLE' if all_ok
          else 'SOME VARIANT STILL MISMATCHES'}")


if __name__ == "__main__":
    main()
