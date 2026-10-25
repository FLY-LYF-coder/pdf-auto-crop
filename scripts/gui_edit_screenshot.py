"""Drive the manual editor with synthetic mouse events and verify the result.

Unit tests cover the geometry, but they cannot prove that the canvas wiring
points the right way round -- a press handler bound to the wrong event, or a
zoom factor inverted, produces perfectly correct ``gui_edit`` calls attached to
the wrong place.  This script drives the *real* window with ``event_generate``,
exactly the way a user moves the mouse, and then checks the exported PDF's
CropBox against the rectangle that was drawn.

It also captures screenshots so the layout can be eyeballed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover
    pass

import pymupdf as fitz  # noqa: E402
from PIL import ImageGrab  # noqa: E402

from pdf_auto_crop import gui, gui_edit  # noqa: E402
from pdf_auto_crop.config import CropConfig  # noqa: E402

SHOTS = ROOT / "diag_out" / "gui"
SHOTS.mkdir(parents=True, exist_ok=True)

FIXTURE = ROOT / "tests" / "fixtures_diag" / "D7_mixed_gaps.pdf"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if condition else 'FAIL'}] {label}{(' -> ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def _pump(app, seconds: float = 8.0) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.update()
        if not app._bg.busy:
            break
        time.sleep(0.02)
    for _ in range(8):
        app.update()
        time.sleep(0.03)


def _shot(app, name: str) -> Path:
    app.update()
    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    path = SHOTS / name
    try:
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(str(path))
    except OSError as exc:
        # A locked or switched-away desktop cannot be grabbed; the checks
        # do not need the picture.
        print(f"(no screenshot for {name}: {exc})")
        path = None
    return path


def _pdf_to_canvas(app, rect: fitz.Rect) -> tuple[int, int, int, int]:
    return app._pdf_rect_to_canvas(rect)


def _drag(app, x0: int, y0: int, x1: int, y1: int) -> None:
    """Press, move, release -- the three events a real drag produces.

    Note the release must be generated as ``<ButtonRelease-1>``: a bare
    ``<Button-1>`` is a *press*, so using it for both ends of the drag would
    silently produce two presses and no release.
    """
    canvas = app.canvas
    canvas.event_generate("<Button-1>", x=x0, y=y0)
    app.update()
    for step in range(1, 5):
        canvas.event_generate(
            "<B1-Motion>",
            x=x0 + (x1 - x0) * step // 4,
            y=y0 + (y1 - y0) * step // 4,
        )
        app.update()
    canvas.event_generate("<ButtonRelease-1>", x=x1, y=y1)
    app.update()


def _cropbox_of(path: Path) -> fitz.Rect:
    """The written CropBox converted back to PDF user space (bottom-up).

    ``get_cropbox`` reports MuPDF's top-down convention; every rect this script
    compares against is bottom-up, exactly like the detector's output.  Reading
    one as the other is what let a mirrored crop look correct.
    """
    doc = fitz.open(str(path))
    try:
        cb = doc[0].cropbox
        h = float(doc[0].mediabox.height)
    finally:
        doc.close()
    return fitz.Rect(cb.x0, h - cb.y1, cb.x1, h - cb.y0)


def _assert_rect(actual, expected, *, abs_tol: float = 1e-6) -> None:
    got, want = tuple(actual), tuple(expected)
    for i, (a, b) in enumerate(zip(got, want)):
        if abs(a - b) > abs_tol:
            print(f"      coord {i}: {got} != {want}")
        assert abs(a - b) <= abs_tol, f"coord {i}: {got} != {want}"


def main() -> int:
    # Every destructive editor action confirms with a modal dialog, which would
    # block this script forever on an unattended run.  Answer "yes" and record
    # what was asked, so the confirmation path itself is still exercised.
    asked: list[str] = []
    gui.messagebox.askyesno = lambda title, msg, **_k: (asked.append(f"{title}: {msg}"), True)[1]

    app = gui.App()
    app.geometry("1320x880+30+30")
    app.update()

    app._set_source(FIXTURE)
    app.var_output.set(str(SHOTS / "manual_out"))
    app._analyse()
    _pump(app)

    detected = len(app._detected)
    print(f"\n1. auto detection")
    print(f"   detected figures: {detected}")
    check("detector found 3 figures on D7", detected == 3, f"got {detected}")
    check("page starts non-manual", not app._manual.is_manual(0))
    check("mode label says auto", "自动检测" in app.lbl_page_mode.cget("text"))

    # -- 1b. THE reported bug: does the drawn frame equal the written one? ---
    print(f"\n1b. auto page: drawn frame vs exported CropBox")
    from pdf_auto_crop.geometry import add_margin_pt
    margin_pt = app._margin_pt()
    check("white border is non-zero", margin_pt > 0, f"{margin_pt:.1f} pt")
    grown = [add_margin_pt(r, margin_pt, app._page_rect()) for r in app._detected]
    bigger = all(g.width * g.height > r.width * r.height for g, r in zip(grown, app._detected))
    check("displayed frames include the border the export applies", bigger,
          f"detected {app._detected[0].width:.0f}x{app._detected[0].height:.0f}pt"
          f" -> shown {grown[0].width:.0f}x{grown[0].height:.0f}pt")
    for i, (g, r) in enumerate(zip(grown, app._detected), start=1):
        _assert_rect(app._display_boxes()[i - 1], g, abs_tol=1e-9)
    check("every displayed frame == detected + border", True)
    auto_out = SHOTS / "auto_out"
    _, auto_n = gui._export_single(FIXTURE, auto_out, app.current_config(), None)
    for i, g in enumerate(grown, start=1):
        _assert_rect(_cropbox_of(auto_out / f"figure_p01_f{i:02d}.pdf"), g, abs_tol=0.01)
    check(f"written CropBox == displayed frame for all {auto_n} auto crops", True)

    # -- 2. enter manual mode; the page should seed from the detection ------
    print(f"\n2. switching to manual mode")
    app.var_edit_mode.set("edit")
    app._on_edit_mode_changed()
    app.update()
    check("page is now manual", app._manual.is_manual(0))
    check(
        "manual boxes seeded from the detector",
        len(app._boxes) == detected,
        f"{len(app._boxes)} boxes",
    )
    check("mode label says manual", "手动框选" in app.lbl_page_mode.cget("text"))

    # -- 3. draw a brand new box by dragging on empty canvas ---------------
    print(f"\n3. drawing a new box by dragging")
    target = fitz.Rect(90.0, 120.0, 300.0, 420.0)
    x0, y0, x1, y1 = _pdf_to_canvas(app, target)
    before = len(app._boxes)
    _drag(app, int(x0), int(y0), int(x1), int(y1))
    check("a box was added", len(app._boxes) == before + 1, f"{before} -> {len(app._boxes)}")
    drawn = app._boxes[-1]
    err = max(
        abs(drawn.x0 - target.x0), abs(drawn.y0 - target.y0),
        abs(drawn.x1 - target.x1), abs(drawn.y1 - target.y1),
    )
    # The drag is issued at integer canvas coordinates, so each edge may be off
    # by up to one screen pixel.  Express the tolerance in *pixels* rather than
    # points: a fixed point tolerance would silently get stricter or looser
    # whenever the window size changes the zoom.
    err_px = err * app._zoom * (CropConfig().dpi / 72.0)
    check(
        "drawn box matches the intended rect (within 1 screen pixel)",
        err_px < 1.5,
        f"max error {err_px:.2f} px = {err:.3f} pt = {err * 25.4 / 72:.4f} mm",
    )
    check("the new box is selected", app._selected == len(app._boxes) - 1)

    # -- 4. move it ---------------------------------------------------------
    print(f"\n4. dragging the box body to move it")

    def _centre(rect):
        a = _pdf_to_canvas(app, rect)
        return int((a[0] + a[2]) / 2), int((a[1] + a[3]) / 2)

    w_before, h_before = drawn.width, drawn.height
    cx, cy = _centre(drawn)
    _drag(app, cx, cy, cx + 40, cy - 25)  # right and up
    moved = app._boxes[-1]
    check(
        "move preserves the box size exactly",
        abs(moved.width - w_before) < 0.01 and abs(moved.height - h_before) < 0.01,
        f"{moved.width:.1f}x{moved.height:.1f}",
    )
    check("move shifted the box right and up", moved.x0 > drawn.x0 and moved.y0 > drawn.y0,
          f"x0 {drawn.x0:.0f}->{moved.x0:.0f}, y0 {drawn.y0:.0f}->{moved.y0:.0f}")

    # -- 5. resize via a corner handle -------------------------------------
    print(f"\n5. dragging the bottom-left corner handle")
    app._selected = len(app._boxes) - 1
    app._refresh_boxes()
    hx, hy = gui_edit.handle_points(moved)[0]  # bottom-left in PDF space
    a = _pdf_to_canvas(app, fitz.Rect(hx, hy, hx, hy))
    hcx, hcy = int((a[0] + a[2]) / 2), int((a[1] + a[3]) / 2)
    _drag(app, hcx, hcy, hcx - 30, hcy + 20)
    resized = app._boxes[-1]
    check(
        "corner drag moved only the left and bottom edges",
        resized.x0 < moved.x0 and abs(resized.x1 - moved.x1) < 0.5
        and abs(resized.y1 - moved.y1) < 0.5 and resized.y0 < moved.y0,
        f"x0 {moved.x0:.0f}->{resized.x0:.0f}, x1 {moved.x1:.0f}->{resized.x1:.0f}, "
        f"y0 {moved.y0:.0f}->{resized.y0:.0f}, y1 {moved.y1:.0f}->{resized.y1:.0f}",
    )

    # -- 6. keyboard nudge --------------------------------------------------
    print(f"\n6. arrow-key nudging")
    app._selected = 0
    app._refresh_boxes()
    base = app._boxes[0]
    app.canvas.focus_set()
    app.canvas.event_generate("<Right>")
    app.update()
    nudged = app._boxes[0]
    check("Right arrow nudges 1 pt", abs(nudged.x0 - (base.x0 + 1.0)) < 1e-6,
          f"{base.x0:.2f} -> {nudged.x0:.2f}")
    app.canvas.event_generate("<Shift-Right>")
    app.update()
    check("Shift+Right nudges 10 pt",
          abs(app._boxes[0].x0 - (nudged.x0 + 10.0)) < 1e-6,
          f"{nudged.x0:.2f} -> {app._boxes[0].x0:.2f}")

    # -- 7. delete one box --------------------------------------------------
    print(f"\n7. deleting a box")
    count_before = len(app._boxes)
    app._selected = 0
    app._refresh_boxes()
    app._delete_selected()
    check("box removed", len(app._boxes) == count_before - 1,
          f"{count_before} -> {len(app._boxes)}")
    check("page stays manual after a delete", app._manual.is_manual(0))

    # -- 8. the boxes that will actually be exported -----------------------
    print(f"\n8. export")
    # THE invariant: every frame the canvas draws must equal the CropBox that
    # lands in the written file.  Measured here rather than assumed.
    shown = app._display_boxes()
    for i, rect in enumerate(shown, start=1):
        _assert_rect(rect, app._boxes[i - 1], abs_tol=1e-9)
    check("canvas frames match the manual store verbatim", True,
          f"{len(shown)} frames, margin={app._margin_pt():.1f}pt")

    overrides = app._manual_overrides()
    check("overrides cover page 0 only", overrides is not None and list(overrides) == [0])

    cfg = app.current_config()
    out = SHOTS / "manual_out"
    folder, count = gui._export_single(FIXTURE, out, cfg, overrides)
    check(f"exported {count} files (one per drawn box)", count == len(shown),
          f"{count} files, {len(shown)} boxes")

    names = sorted(p.name for p in folder.glob("*.pdf"))
    check("naming follows the CLI convention",
          all(n.startswith("figure_p01_f") for n in names), str(names[:3]))

    # Every exported crop must equal the rectangle the user drew, verbatim.
    ok = True
    for i, rect in enumerate(shown, start=1):
        f = folder / f"figure_p01_f{i:02d}.pdf"
        if not f.exists():
            ok = False
            continue
        got = tuple(_cropbox_of(f))
        if max(abs(a - b) for a, b in zip(got, tuple(rect))) > 0.01:
            ok = False
            print(f"      box {i}: cropbox {got} != drawn {tuple(rect)}")
    check("every CropBox equals the on-screen frame (no margin applied)", ok)

    # -- 9. reset back to the detector --------------------------------------
    print(f"\n9. restoring the automatic result")
    app._reset_page_to_detected()
    check("page is automatic again", not app._manual.is_manual(0))
    check("boxes are the detector's again", len(app._boxes) == detected,
          f"{len(app._boxes)} vs {detected}")
    check("overrides are gone", app._manual_overrides() is None)
    check("destructive reset asked for confirmation first", bool(asked),
          asked[0] if asked else "no dialog shown")

    # -- 10. vector preservation -------------------------------------------
    print(f"\n10. vector preservation through the manual path")
    vec_src = ROOT / "tests" / "fixtures" / "case7_vector_only.pdf"
    vec_out = SHOTS / "manual_vec"
    gui._export_single(vec_src, vec_out, CropConfig(),
                       {0: [fitz.Rect(40, 60, 520, 760)]})
    doc = fitz.open(str(vec_out / "figure_p01_f01.pdf"))
    try:
        n_img, n_draw = len(doc[0].get_images()), len(doc[0].get_drawings())
    finally:
        doc.close()
    check("no rasterisation was introduced", n_img == 0 and n_draw > 0,
          f"{n_img} images, {n_draw} drawings")

    # -- screenshots --------------------------------------------------------
    # Auto page first: this is the state where the solid frame (crop window,
    # border included) and the dashed frame (raw content) differ.
    app.var_edit_mode.set("view")
    app._on_edit_mode_changed()
    p_auto = _shot(app, "gui_view_mode.png")

    app.var_edit_mode.set("edit")
    app._on_edit_mode_changed()
    app._selected = 0
    app._refresh_boxes()
    p_manual = _shot(app, "gui_manual_edit.png")
    print(f"\nscreenshots: {p_auto}\n              {p_manual}")

    app._on_close()
    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
