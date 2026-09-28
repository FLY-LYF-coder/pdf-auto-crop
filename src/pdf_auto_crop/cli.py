"""Command-line interface for pdf-auto-crop.

Run with::

    # Wizard (default when no args)
    python -m pdf_auto_crop.cli

    # Single file fast mode
    python -m pdf_auto_crop.cli --input input.pdf --preset tight

    # Batch processing (directory or glob)
    python -m pdf_auto_crop.cli --input ./pdfs/ --preset standard

    # Interactive preview-confirm-execute
    python -m pdf_auto_crop.cli --input input.pdf --interactive

The CLI is intentionally thin -- every flag delegates to the
:mod:`pdf_auto_crop` library so behaviour stays consistent between scripting
and interactive use.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, ui_utils
from .batch import collect_pdfs, process_batch
from .config import CropConfig
from .interactive import run_wizard
from .logging_setup import configure_logging, get_logger
from .pipeline import resolve_paths, run_pipeline
from .presets import apply_preset, get_preset_info

log = get_logger("cli")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf-auto-crop",
        description=(
            "Split a multi-figure PDF (typically exported from PowerPoint) into "
            "single-figure PDFs while preserving vector content.  "
            "Run without --input to start the interactive wizard."
        ),
    )
    parser.add_argument(
        "--input",
        required=False,
        default=None,
        type=Path,
        help=(
            "Path to a source PDF, a directory containing PDFs, or a glob "
            "pattern.  When omitted the interactive wizard starts."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./output"),
        help="Output directory (default: ./output).",
    )
    parser.add_argument(
        "--preset",
        choices=["tight", "standard", "loose"],
        default=None,
        help="Apply a named preset (tight/standard/loose).",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help=(
            "Single-file interactive mode: render preview, show detected "
            "figures, then ask the user to confirm or adjust before cropping."
        ),
    )
    parser.add_argument(
        "--batch-recursive",
        action="store_true",
        help="When --input is a directory, recurse into subdirectories.",
    )
    parser.add_argument(
        "--batch-flat",
        action="store_true",
        help=(
            "Use a flat output layout (no per-PDF subdirectories); "
            "outputs go directly into --output with per-file prefix."
        ),
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=None,
        help="White margin (mm) added around each figure (default 3).",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="Binarisation threshold 0-255 (default 245).",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=None,
        help="Render DPI for analysis (default 250).",
    )
    parser.add_argument(
        "--min-area",
        type=float,
        default=None,
        help="Minimum figure area in mm^2 (default 25).",
    )
    parser.add_argument(
        "--min-width",
        type=float,
        default=None,
        help="Minimum figure width in mm (default 10).",
    )
    parser.add_argument(
        "--min-height",
        type=float,
        default=None,
        help="Minimum figure height in mm (default 10).",
    )
    parser.add_argument(
        "--morph-kernel-multiplier",
        type=float,
        default=None,
        help="Adaptive close kernel multiplier (default 1.0).",
    )
    parser.add_argument(
        "--merge-gap-mm",
        type=float,
        default=None,
        help="Merge components whose dilated rectangles overlap (default 1.5mm).",
    )
    parser.add_argument(
        "--padding-mm",
        type=float,
        default=None,
        help="Extra padding (mm) applied on top of --margin (default 0).",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Also save preview PNGs alongside the cropped PDFs.",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="Save preview PNGs only; do not write cropped PDFs.",
    )
    parser.add_argument(
        "--algorithm",
        choices=["smart", "legacy"],
        default=None,
        help=(
            "Detection algorithm. 'smart' (default) adapts the closing "
            "kernel and merge threshold to each page's measured layout; "
            "'legacy' uses the original fixed-parameter detector."
        ),
    )
    parser.add_argument(
        "--naming",
        choices=["per-page", "flat"],
        default=None,
        help="Output filename strategy (default: per-page).",
    )
    parser.add_argument(
        "--output-prefix",
        type=str,
        default=None,
        help="Output filename prefix (default: figure).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG/INFO/WARNING/ERROR, default INFO).",
    )
    parser.add_argument(
        "--save-config",
        action="store_true",
        help="Save the effective config as JSON in the output directory.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Load a JSON config file that overrides the defaults.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colour output.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"pdf-auto-crop {__version__}",
    )
    return parser


def _resolve_config(args: argparse.Namespace) -> CropConfig:
    """Build a :class:`CropConfig` honouring CLI overrides + optional JSON file."""
    if args.config is not None:
        if not args.config.exists():
            raise FileNotFoundError(f"Config file not found: {args.config}")
        cfg = CropConfig.from_json_file(args.config)
        log.info("Loaded config from %s", args.config)
    else:
        cfg = CropConfig()

    if args.preset:
        cfg = apply_preset(cfg, args.preset)
        info = get_preset_info(args.preset)
        log.info("Applied preset %r: %s", args.preset, info["description"])

    overrides = {
        "margin_mm": args.margin,
        "threshold": args.threshold,
        "dpi": args.dpi,
        "min_area_mm2": args.min_area,
        "min_width_mm": args.min_width,
        "min_height_mm": args.min_height,
        "morph_kernel_multiplier": args.morph_kernel_multiplier,
        "merge_gap_mm": args.merge_gap_mm,
        "padding_mm": args.padding_mm,
        "naming": args.naming,
        "algorithm": args.algorithm,
        "output_prefix": args.output_prefix,
    }
    cleaned = {k: v for k, v in overrides.items() if v is not None}
    if cleaned:
        cfg = CropConfig.from_dict({**cfg.to_dict(), **cleaned})
        log.info("Applied CLI overrides: %s", ", ".join(f"{k}={v}" for k, v in cleaned.items()))

    cfg.validate()
    return cfg


def run(args: argparse.Namespace) -> int:
    """Execute the pipeline on a single PDF.  Returns an exit code."""
    cfg = _resolve_config(args)
    return run_pipeline(args, cfg)


def _is_batch_input(path: Path) -> bool:
    """A path counts as batch when it is a directory or a glob pattern."""
    if path is None:
        return False
    if path.exists() and path.is_dir():
        return True
    return any(ch in str(path) for ch in "*?[")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    ui_utils.enable_ansi()
    parser = _build_parser()

    if argv is None:
        argv = sys.argv[1:]

    if "--no-color" in argv:
        os.environ["NO_COLOR"] = "1"

    if not argv:
        # No args -> wizard.
        try:
            return run_wizard()
        except KeyboardInterrupt:
            print()
            ui_utils.warn("Interrupted by user.")
            return 130

    args = parser.parse_args(argv)

    if getattr(args, "no_color", False):
        os.environ["NO_COLOR"] = "1"

    configure_logging(args.log_level)

    if args.input is None:
        # Args present but no --input -> wizard.
        try:
            return run_wizard()
        except KeyboardInterrupt:
            print()
            ui_utils.warn("Interrupted by user.")
            return 130

    try:
        if _is_batch_input(args.input):
            return _run_cli_batch(args)
        return run(args)
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        return 130
    except Exception as exc:  # pragma: no cover -- top-level safety net.
        log.exception("Unhandled error: %s", exc)
        return 1


def _run_cli_batch(args: argparse.Namespace) -> int:
    """Top-level batch handler: scan inputs and process each PDF."""
    cfg = _resolve_config(args)
    pdfs = collect_pdfs(
        [args.input],
        recursive=args.batch_recursive,
    )
    if not pdfs:
        ui_utils.error(f"No PDFs found at {args.input}.")
        return 2
    if not args.output.exists():
        args.output.mkdir(parents=True, exist_ok=True)
    ui_utils.info(f"Batch: {len(pdfs)} PDF(s) -> {args.output}")
    result = process_batch(
        pdfs,
        args.output,
        cfg=cfg,
        args=args,
        flat_layout=args.batch_flat,
    )
    _report_batch(result)
    return 0 if result.failed == 0 else 4


def _report_batch(result) -> None:
    ui_utils.section("Batch summary")
    print(f"  Total:     {result.total}")
    print(f"  Succeeded: {ui_utils.colorize(str(result.succeeded), ui_utils.C.GREEN)}")
    print(f"  Failed:    {ui_utils.colorize(str(result.failed), ui_utils.C.RED)}")
    print(f"  Skipped:   {result.skipped}")
    if result.failures:
        ui_utils.warn("Failures:")
        for f in result.failures:
            print(f"    - {f}")


__all__ = ["main", "run", "_build_parser", "_resolve_config", "resolve_paths"]


if __name__ == "__main__":
    sys.exit(main())