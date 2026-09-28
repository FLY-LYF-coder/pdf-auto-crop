"""Tests for the graphical front-end.

The tkinter widget tree cannot be exercised in a headless test run, so the
testable surface is split in two:

* :mod:`pdf_auto_crop.gui_hints` -- the detection-feedback engine, which has no
  tkinter import at all and is fully covered here.
* :mod:`pdf_auto_crop.gui` -- the window itself.  Only its *pure* helpers and
  its worker bodies are tested; they are deliberately written to touch neither
  Tk nor the filesystem outside the temporary directory.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
import pytest

from pdf_auto_crop import gui_hints
from pdf_auto_crop.config import CropConfig

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
FIXTURES_DIAG = ROOT / "fixtures_diag"


def _rect(x0: float, y0: float, x1: float, y1: float) -> fitz.Rect:
    return fitz.Rect(x0, y0, x1, y1)


#: An A4 page: 595 x 842 pt == ~623 x 881 mm^2.
A4_MM2 = 595 * 842 * (25.4 / 72.0) ** 2


# ---------------------------------------------------------------------------
# gui_hints.analyze_page
# ---------------------------------------------------------------------------


def test_blank_page_is_not_reported_as_failure() -> None:
    """A page with no ink yields no figures -- that is correct, not an error."""
    result = gui_hints.analyze_page(
        figure_count=0, has_ink=False, page_area_mm2=A4_MM2
    )
    assert result.figure_count == 0
    assert result.hints[0].level == gui_hints.LEVEL_WARN
    assert result.hints[0].level != gui_hints.LEVEL_ERROR
    assert result.suggested_config(CropConfig()) is None


def test_no_figures_on_inked_page_is_an_error_with_a_fix() -> None:
    result = gui_hints.analyze_page(
        figure_count=0, has_ink=True, page_area_mm2=A4_MM2, config=CropConfig()
    )
    assert result.level == gui_hints.LEVEL_ERROR
    hint = result.hints[0]
    assert hint.is_actionable
    fixed = result.suggested_config(CropConfig())
    assert fixed is not None
    assert fixed.threshold < CropConfig().threshold


def test_healthy_page_produces_no_hints() -> None:
    rects = [_rect(50, 50, 300, 400)] * 2
    result = gui_hints.analyze_page(
        figure_count=2,
        has_ink=True,
        page_area_mm2=A4_MM2,
        rects=rects,
        config=CropConfig(),
    )
    assert result.hints == ()
    assert result.level == gui_hints.LEVEL_OK
    assert result.suggested_config(CropConfig()) is None


def test_subfigure_grid_is_not_flagged_as_fragmented() -> None:
    """A 2x2 panel grid is legitimately small -- must not raise a warning.

    D3 detects 1 big panel plus 4 panels of 45x60 mm.  Each panel is only
    ~4% of an A4 page, so a page-relative "too small" rule would call a
    perfectly correct result fragmented.
    """
    mm = 72.0 / 25.4
    panels = [_rect(0, 0, 45 * mm, 60 * mm)] * 4
    panels.append(_rect(0, 0, 80 * mm, 130 * mm))
    result = gui_hints.analyze_page(
        figure_count=5,
        has_ink=True,
        page_area_mm2=A4_MM2,
        rects=panels,
        config=CropConfig(),
    )
    assert result.level == gui_hints.LEVEL_OK, result.headline
    assert result.suggested_config(CropConfig()) is None


def test_fragments_are_detected_and_suggest_larger_minimums() -> None:
    """A pile of tiny boxes is fragmentation, not a real figure count."""
    small = _rect(0, 0, 20 * 72 / 25.4, 20 * 72 / 25.4)  # 20x20 mm
    result = gui_hints.analyze_page(
        figure_count=8,
        has_ink=True,
        page_area_mm2=A4_MM2,
        rects=[small] * 8,
        config=CropConfig(),
    )
    assert result.level == gui_hints.LEVEL_WARN
    hint = result.hints[0]
    assert "8" in hint.title
    fixed = result.suggested_config(CropConfig())
    assert fixed is not None
    assert fixed.min_area_mm2 > CropConfig().min_area_mm2
    assert fixed.min_width_mm > CropConfig().min_width_mm
    assert fixed.min_height_mm > CropConfig().min_height_mm


def test_single_figure_swallowing_the_page_is_flagged_without_a_fix() -> None:
    """Merging everything into one box is ambiguous -- advise, do not guess."""
    result = gui_hints.analyze_page(
        figure_count=1,
        has_ink=True,
        page_area_mm2=A4_MM2,
        rects=[_rect(5, 5, 590, 837)],
        config=CropConfig(),
    )
    assert result.level == gui_hints.LEVEL_WARN
    assert "整页" in result.hints[0].title
    assert not result.hints[0].is_actionable
    assert result.suggested_config(CropConfig()) is None


def test_many_small_figures_plus_a_page_swallower_reports_worst_first() -> None:
    mm = 72.0 / 25.4
    rects = [_rect(0, 0, 20 * mm, 20 * mm)] * 6 + [_rect(0, 0, 594, 841)]
    result = gui_hints.analyze_page(
        figure_count=7,
        has_ink=True,
        page_area_mm2=A4_MM2,
        rects=rects,
        config=CropConfig(),
    )
    levels = [h.level for h in result.hints]
    assert levels == sorted(levels, key=lambda lv: 0 if lv == gui_hints.LEVEL_ERROR else 1)
    assert result.headline == result.hints[0].title


def test_suggested_fix_step_is_capped() -> None:
    """One click must never nuke a parameter the user set deliberately.

    With ``min_area_mm2`` already large the raw suggestion (x1.8) would jump
    a long way, so the step is clamped to :data:`gui_hints.MAX_STEP`.
    """
    tuned = CropConfig(min_area_mm2=2000.0)
    result = gui_hints.analyze_page(
        figure_count=9,
        has_ink=True,
        page_area_mm2=A4_MM2,
        rects=[_rect(0, 0, 20 * 72 / 25.4, 20 * 72 / 25.4)] * 9,
        config=tuned,
    )
    fixed = result.suggested_config(tuned)
    assert fixed is not None
    # Fragmentation means "too many pieces" -> the minimum must go *up*...
    assert fixed.min_area_mm2 > tuned.min_area_mm2
    # ...but only by one capped step, not all the way to x1.8.
    assert fixed.min_area_mm2 == pytest.approx(
        tuned.min_area_mm2 + gui_hints.MAX_STEP["min_area_mm2"]
    )


def test_suggested_config_is_always_valid() -> None:
    """Whatever we suggest must survive CropConfig.validate()."""
    for count, rects in [
        (0, []),
        (3, [_rect(50, 50, 400, 600)] * 3),
        (10, [_rect(0, 0, 20 * 72 / 25.4, 20 * 72 / 25.4)] * 10),
    ]:
        result = gui_hints.analyze_page(
            figure_count=count,
            has_ink=True,
            page_area_mm2=A4_MM2,
            rects=rects,
            config=CropConfig(),
        )
        candidate = result.suggested_config(CropConfig())
        if candidate is not None:
            candidate.validate()


def test_summarise_figures_formats_millimetres() -> None:
    text = gui_hints.summarise_figures([_rect(0, 0, 80 * 72 / 25.4, 60 * 72 / 25.4)])
    assert "80x60" in text
    assert text.endswith("mm")


def test_summarise_figures_elides_long_lists() -> None:
    rects = [_rect(0, 0, 100, 100)] * 12
    text = gui_hints.summarise_figures(rects, limit=3)
    assert "共12个" in text
    assert gui_hints.summarise_figures([]) == ""


# ---------------------------------------------------------------------------
# gui.py pure helpers and worker bodies
# ---------------------------------------------------------------------------


def test_gui_module_imports_without_a_display() -> None:
    """Importing the window module must not require an X server / desktop."""
    from pdf_auto_crop import gui

    assert hasattr(gui, "App")
    assert hasattr(gui, "launch")
    assert callable(gui.build_pipeline_args)


def test_build_pipeline_args_satisfies_run_pipeline() -> None:
    from pdf_auto_crop.gui import build_pipeline_args

    args = build_pipeline_args(Path("a.pdf"), Path("out"))
    for attr in (
        "input", "output", "preview", "preview_only", "interactive",
        "save_config", "output_prefix",
    ):
        assert hasattr(args, attr), f"run_pipeline reads args.{attr}"


def test_page_area_mm2_matches_a4() -> None:
    from pdf_auto_crop.gui import page_area_mm2

    assert page_area_mm2((595, 842)) == pytest.approx(A4_MM2)


def test_export_single_matches_cli_output(tmp_path: Path) -> None:
    """The GUI's export must produce exactly what the CLI produces."""
    from pdf_auto_crop.gui import _export_single

    src = FIXTURES_DIAG / "D7_mixed_gaps.pdf"
    out = tmp_path / "gui"
    cfg = CropConfig()
    folder, count = _export_single(src, out, cfg)
    assert count == 3
    assert sorted(p.name for p in folder.glob("*.pdf")) == [
        "figure_p01_f01.pdf",
        "figure_p01_f02.pdf",
        "figure_p01_f03.pdf",
    ]


def test_export_single_preserves_vector(tmp_path: Path) -> None:
    from pdf_auto_crop.gui import _export_single

    src = FIXTURES / "case7_vector_only.pdf"
    out = tmp_path / "vec"
    _, count = _export_single(src, out, CropConfig())
    assert count == 1
    doc = fitz.open(str(out / "figure_p01_f01.pdf"))
    try:
        page = doc[0]
        assert len(page.get_images()) == 0
        assert len(page.get_drawings()) > 0
    finally:
        doc.close()


def test_export_batch_uses_per_file_subdirectories(tmp_path: Path) -> None:
    from pdf_auto_crop.gui import _export_batch

    pdfs = sorted(FIXTURES_DIAG.glob("D[124]*.pdf"))
    out = tmp_path / "batch"
    messages: list[str] = []
    folder, total = _export_batch(pdfs, out, CropConfig(), messages.append)
    assert total == 3 + 4 + 1
    for pdf in pdfs:
        assert (folder / pdf.stem).is_dir()
    assert len(messages) == len(pdfs)


def test_analyse_page_worker_returns_rects(tmp_path: Path) -> None:
    from pdf_auto_crop.gui import _analyse_page

    doc = fitz.open(str(FIXTURES_DIAG / "D7_mixed_gaps.pdf"))
    try:
        rgb, size_pt, rects = _analyse_page(doc, 0, CropConfig())
    finally:
        doc.close()
    assert rgb.ndim == 3 and rgb.shape[2] == 3
    assert size_pt[0] > 0 and size_pt[1] > 0
    assert len(rects) == 3


def test_analyse_page_worker_honours_legacy_algorithm() -> None:
    from pdf_auto_crop.gui import _analyse_page

    doc = fitz.open(str(FIXTURES_DIAG / "D7_mixed_gaps.pdf"))
    try:
        _, _, rects = _analyse_page(doc, 0, CropConfig(algorithm="legacy"))
    finally:
        doc.close()
    assert len(rects) == 2


def test_analyse_page_worker_rejects_closed_document() -> None:
    from pdf_auto_crop.gui import _analyse_page

    with pytest.raises(ValueError):
        _analyse_page(None, 0, CropConfig())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def test_main_falls_back_to_wizard_without_tty(monkeypatch) -> None:
    """Redirected stdin must keep the old behaviour, not launch a window."""
    from pdf_auto_crop import __main__ as entry

    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    called = {"wizard": False, "gui": False}
    monkeypatch.setattr(entry, "run_wizard", lambda: called.__setitem__("wizard", True) or 0)
    assert entry._ask_mode() == "wizard"


def test_main_chooses_gui_by_default(monkeypatch) -> None:
    from pdf_auto_crop import __main__ as entry

    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(entry.ui_utils, "prompt_line", lambda *_a, **_k: "1")
    assert entry._ask_mode() == "gui"
    monkeypatch.setattr(entry.ui_utils, "prompt_line", lambda *_a, **_k: "2")
    assert entry._ask_mode() == "wizard"


def test_gui_unavailable_falls_back_to_wizard(monkeypatch) -> None:
    from pdf_auto_crop import __main__ as entry

    monkeypatch.setattr(entry, "_gui_available", lambda: False)
    assert entry._ask_mode() == "wizard"
