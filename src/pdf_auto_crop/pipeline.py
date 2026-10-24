"""Single-file crop pipeline -- used by both CLI and batch layers.

Extracted from :mod:`pdf_auto_crop.cli` so the batch module can reuse it
without creating an import cycle.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import pymupdf as fitz

from .config import CropConfig
from .detector import detect_figures
from .geometry import add_margin_pt, mm_to_pt
from . import ui_utils
from .logging_setup import get_logger
from .namer import flat_name, per_page_name
from .pdf_processor import crop_and_save_pdf, render_page_to_array
from .preview import draw_preview, save_preview
from .smart_detector import smart_detect_figures

log = get_logger("pipeline")


def get_detector(config: CropConfig):
    """Return the detection callable for ``config``.

    ``smart`` (the default) adapts the closing kernel and merge threshold to
    each page's measured layout; ``legacy`` restores the original
    fixed-parameter detector.  Both share an identical signature and return
    PDF-space rectangles, so the pipeline is agnostic to the choice.
    """
    if config.algorithm == "legacy":
        return detect_figures
    return smart_detect_figures


def resolve_paths(raw: Path) -> Path:
    """Resolve relative paths against the script location (not CWD)."""
    if raw.is_absolute():
        return raw
    if raw.exists():
        return raw.resolve()
    script_dir = Path(__file__).resolve().parent.parent.parent
    candidate = (script_dir / raw).resolve()
    return candidate if candidate.exists() else raw.resolve()


def _iter_pages(doc: fitz.Document, only: Iterable[int] | None = None) -> Iterable[int]:
    if only is None:
        return range(len(doc))
    return [i for i in only if 0 <= i < len(doc)]


def _interactive_confirm(figure_count: int, page_no: int) -> str:
    """Ask the user whether to crop / skip / quit after a preview."""
    ui_utils.info(
        f"Page {page_no}: detected {figure_count} figure(s)."
    )
    # Use a letter-aware prompt so users can type ``y``/``n``/``q``.
    while True:
        raw = ui_utils.prompt_line(
            "Detection looks correct? [Y] crop  /  [n] skip  /  [q] quit",
            default="y",
        ).strip().lower()
        if raw in ("y", "yes", "1", ""):
            return "y"
        if raw in ("n", "no", "2"):
            return "n"
        if raw in ("q", "quit", "exit", "3"):
            return "q"
        ui_utils.warn(f"'{raw}' is not a valid choice; please answer y/n/q.")


def _with_axis_labels(
    doc: fitz.Document,
    page_index: int,
    rects: Sequence[fitz.Rect],
    page_h_pt: float,
) -> List[fitz.Rect]:
    """Grow the detected rectangles to include their own axis labels.

    The detector works on the raster and finds the ink of the plot.  The axis
    ticks, the axis titles and the figure's title are separate ink
    components just outside the frame, so they are not in the detected box
    -- and a crop taken from that box loses them.

    The rectangles come bottom-up (PDF user space) and the page's text is
    reported top-down, so both are converted through the same flip.
    """
    if not rects:
        return list(rects)
    from .figure_text import expand_regions_with_text, text_blocks

    top_down = [
        fitz.Rect(r.x0, page_h_pt - r.y1, r.x1, page_h_pt - r.y0)
        for r in rects
    ]
    grown = expand_regions_with_text(top_down, text_blocks(doc[page_index]))
    return [
        fitz.Rect(r.x0, page_h_pt - r.y1, r.x1, page_h_pt - r.y0)
        for r in grown
    ]


def run_pipeline(
    args: argparse.Namespace,
    cfg: CropConfig,
    *,
    interactive_prompt=None,
    manual_boxes: Optional[Mapping[int, Sequence[fitz.Rect]]] = None,
    report: Optional[Dict[str, Any]] = None,
) -> int:
    """Run the crop pipeline on a single PDF.

    Args:
        args: ``argparse.Namespace`` whose relevant fields (``input``,
            ``output``, ``preview``, ``preview_only``, ``interactive``,
            ``output_prefix``, ``save_config``) are honoured.
        cfg: Active :class:`CropConfig`.
        interactive_prompt: Optional override of the per-page interactive
            prompt -- takes ``(figure_count, page_no)`` and returns one of
            ``"y"``, ``"n"``, ``"q"``.  When ``None``, the built-in
            :func:`ui_utils.menu` is used.
        manual_boxes: Optional per-page rectangle overrides keyed by zero-based
            page index.  Pages present in the mapping are cropped with exactly
            the rectangles given -- the GUI's manual editor supplies these for
            the pages the user redrew.  Pages *absent* from the mapping still go
            through the detector, which is what makes the GUI hybrid rather than
            all-or-nothing.  ``None`` (the default, and therefore the entire CLI
            path) never consults this argument at all.
        report: Optional dict, populated in place on success with
            ``{"total": int, "per_page": list[int], "files": list[str]}``.
            The GUI needs the authoritative list of what was written: counting
            ``*.pdf`` in the output directory would also count files left over
            from an earlier, differently-parameterised run, and the status bar
            would then claim a number the user cannot account for.

    Returns:
        Process exit code (``0`` on success).
    """
    confirm_fn = interactive_prompt or _interactive_confirm

    input_path = resolve_paths(args.input)
    if not input_path.exists():
        log.error("Input PDF does not exist: %s", input_path)
        return 2
    if not input_path.is_file():
        log.error("Input path is not a file: %s", input_path)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)

    if args.save_config:
        cfg.write_json(args.output / "config.json")
        log.info("Saved config to %s", args.output / "config.json")

    if args.preview_only:
        log.info("Preview-only mode: cropped PDFs will NOT be written.")

    log.info(
        "Processing %s (algorithm=%s dpi=%d threshold=%d margin=%.1fmm merge_gap=%.1fmm)",
        input_path.name,
        cfg.algorithm,
        cfg.dpi,
        cfg.threshold,
        cfg.margin_mm,
        cfg.merge_gap_mm,
    )

    detect = get_detector(cfg)

    doc = fitz.open(str(input_path))
    try:
        if len(doc) == 0:
            log.error("Input PDF has no pages.")
            return 3
        total_figures = 0
        global_idx = 0
        per_page_counts: List[int] = []
        written: List[str] = []

        for page_index in _iter_pages(doc):
            log.info("--- Page %d/%d ---", page_index + 1, len(doc))
            rgb, (page_w_pt, page_h_pt) = render_page_to_array(doc, page_index, cfg.dpi)
            page_rect = fitz.Rect(0, 0, page_w_pt, page_h_pt)

            if manual_boxes is not None and page_index in manual_boxes:
                # The user drew these, so they are already final: no detector,
                # and no margin re-applied on top of a deliberate crop.
                rects = [fitz.Rect(r) for r in manual_boxes[page_index]]
                margin_pt = 0.0
                log.info(
                    "Using %d manual rectangle(s) for page %d",
                    len(rects), page_index + 1,
                )
            else:
                rects = detect(rgb, (page_w_pt, page_h_pt), cfg)
                # The detector finds the *ink* of a plot.  The axis ticks,
                # the axis titles and the figure's title sit a millimetre or
                # two outside the frame, so they are folded back in from the
                # page's text -- otherwise a curve plot is cropped without
                # its axes.
                rects = _with_axis_labels(doc, page_index, rects, page_h_pt)
                margin_pt = mm_to_pt(cfg.margin_mm + cfg.padding_mm)
                log.info("Detected %d figure(s) on page %d", len(rects), page_index + 1)
            per_page_counts.append(len(rects))

            if args.preview or args.preview_only:
                preview = draw_preview(
                    rgb,
                    rects,
                    dpi=cfg.dpi,
                    labels=[per_page_name(page_index, i, cfg.output_prefix) for i in range(len(rects))]
                    if cfg.naming == "per-page"
                    else [flat_name(global_idx + i, cfg.output_prefix) for i in range(len(rects))],
                )
                preview_name = f"{cfg.output_prefix}_preview_p{page_index + 1:02d}.png"
                save_preview(preview, args.output / preview_name)
                log.info("Saved preview %s", preview_name)

            if args.preview_only:
                continue

            if args.interactive:
                verdict = confirm_fn(len(rects), page_index + 1)
                if verdict == "q":
                    log.warning("Interactive: aborted by user.")
                    return 0
                if verdict == "n":
                    log.info("Interactive: skipped page %d.", page_index + 1)
                    continue

            for fig_idx, rect in enumerate(rects):
                expanded = add_margin_pt(rect, margin_pt, page_rect)
                if cfg.naming == "per-page":
                    out_name = per_page_name(page_index, fig_idx, cfg.output_prefix)
                else:
                    out_name = flat_name(global_idx, cfg.output_prefix)
                out_path = args.output / out_name
                try:
                    crop_and_save_pdf(input_path, page_index, expanded, out_path)
                except Exception as exc:
                    log.error("Failed to save %s: %s", out_name, exc)
                    return 4
                log.info("Saved %s (%.1fx%.1f pt)", out_name, expanded.width, expanded.height)
                written.append(out_name)
                global_idx += 1
                total_figures += 1

        log.info(
            "Done. %d figure(s) saved across %d page(s); per-page counts=%s",
            total_figures,
            len(per_page_counts),
            per_page_counts,
        )
        if report is not None:
            report.update(
                {
                    "total": total_figures,
                    "per_page": list(per_page_counts),
                    "files": list(written),
                }
            )
        return 0
    finally:
        doc.close()


__all__ = ["run_pipeline", "resolve_paths", "get_detector"]