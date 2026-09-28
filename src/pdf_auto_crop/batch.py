"""Batch processing helpers -- scan directories and process many PDFs.

The public surface is intentionally small:

* :func:`collect_pdfs` -- turn an input argument (file, directory, glob) into
  a concrete list of PDF paths.
* :func:`process_batch` -- run :func:`pdf_auto_crop.pipeline.run_pipeline` on
  each PDF with a unified output directory and progress reporting.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Union

from . import ui_utils
from .pipeline import run_pipeline


# Type alias used throughout the public surface.
InputPath = Union[str, os.PathLike, Path]


# ---------------------------------------------------------------------------
# PDF collection.


def collect_pdfs(
    inputs: Union[InputPath, Sequence[InputPath]],
    *,
    recursive: bool = False,
    pattern: str = "*.pdf",
) -> List[Path]:
    """Return a sorted list of PDF files for the given input argument(s).

    Args:
        inputs: One or more paths.  Each entry may be:
            * A single PDF file.
            * A directory (scanned for ``*.pdf``).
            * A glob pattern (``*``/``?``) interpreted by :mod:`fnmatch`.
        recursive: When ``True``, descend into subdirectories of directories.
        pattern: Glob pattern used inside directories (default ``*.pdf``).

    Returns:
        Sorted list of unique :class:`Path` objects pointing at PDF files.
        Order is deterministic for reproducible test runs.
    """
    if isinstance(inputs, (str, os.PathLike)):
        inputs_seq: Sequence[InputPath] = [inputs]
    else:
        inputs_seq = list(inputs)

    seen: set = set()
    found: List[Path] = []

    for raw in inputs_seq:
        path = Path(raw)
        if not _has_glob_magic(str(raw)) and path.exists():
            if path.is_file():
                if path.suffix.lower() == ".pdf":
                    if path not in seen:
                        seen.add(path)
                        found.append(path)
                continue
            if path.is_dir():
                _collect_from_dir(path, found, seen, recursive=recursive, pattern=pattern)
                continue

        # Either a glob pattern, or a non-existent path treated as glob.
        for hit in _glob_pdfs(path, recursive=recursive, pattern=pattern):
            if hit not in seen:
                seen.add(hit)
                found.append(hit)

    found.sort()
    return found


def _has_glob_magic(s: str) -> bool:
    return any(ch in s for ch in "*?[")


def _collect_from_dir(
    directory: Path,
    out: List[Path],
    seen: set,
    *,
    recursive: bool,
    pattern: str,
) -> None:
    iterator: Iterable[Path]
    if recursive:
        iterator = directory.rglob(pattern)
    else:
        iterator = directory.glob(pattern)
    for hit in iterator:
        if hit.is_file() and hit.suffix.lower() == ".pdf" and hit not in seen:
            seen.add(hit)
            out.append(hit)


def _glob_pdfs(
    path: Path,
    *,
    recursive: bool,
    pattern: str,
) -> Iterable[Path]:
    """Interpret ``path`` as a glob pattern (or anchor) and yield PDF hits."""
    raw = str(path)
    parent = path.parent if path.parent != Path("") else Path(".")
    candidates: Iterable[Path]
    if _has_glob_magic(raw):
        candidates = parent.glob(path.name)
    elif path.exists() and path.is_dir():
        candidates = _iter_dir(path, recursive=recursive, pattern=pattern)
    else:
        candidates = ()
    for hit in candidates:
        if hit.is_file() and hit.suffix.lower() == ".pdf":
            yield hit


def _iter_dir(directory: Path, *, recursive: bool, pattern: str) -> Iterable[Path]:
    if recursive:
        return directory.rglob(pattern)
    return directory.glob(pattern)


# ---------------------------------------------------------------------------
# Batch execution.


@dataclass
class BatchResult:
    """Summary of a batch run."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    outputs: List[Path] = field(default_factory=list)
    failures: List[Path] = field(default_factory=list)


def process_batch(
    pdfs: Sequence[Path],
    output_dir: Path,
    *,
    cfg,
    args,
    log: Optional[logging.Logger] = None,
    flat_layout: bool = False,
) -> BatchResult:
    """Process each PDF in ``pdfs`` independently.

    Each PDF is routed to its own subdirectory (``<output>/<stem>/``) by
    default; pass ``flat_layout=True`` to drop all outputs into
    ``output_dir`` with a per-file prefix instead.

    Args:
        pdfs: PDFs to process.
        output_dir: Root output directory (created if missing).
        cfg: A :class:`pdf_auto_crop.config.CropConfig` shared by all jobs.
        args: An ``argparse.Namespace`` whose other fields (``preview``,
            ``preview_only``, ``save_config``) are honoured.  ``input`` and
            ``output`` are overridden per iteration.
        log: Optional logger; falls back to ``logging.getLogger("pdf_auto_crop.batch")``.
        flat_layout: When ``True`` use a flat layout with ``<stem>_<page>_<fig>``
            naming instead of per-PDF subdirectories.
    """
    if log is None:
        log = logging.getLogger("pdf_auto_crop.batch")
    if not pdfs:
        return BatchResult()

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = BatchResult(total=len(pdfs))
    log.info("Batch start: %d PDF(s) -> %s", len(pdfs), output_dir)

    for idx, pdf in enumerate(pdfs, start=1):
        if not pdf.exists():
            ui_utils.warn(f"[{idx}/{len(pdfs)}] missing: {pdf}")
            result.skipped += 1
            continue

        if flat_layout:
            job_output = output_dir
            job_prefix = f"{pdf.stem}_"
            # When dropping all outputs into a single flat directory we use
            # ``flat`` naming so the per-PDF counter resets; the per-file
            # ``job_prefix`` differentiates filenames across inputs.
            job_naming = "flat"
        else:
            job_output = output_dir / pdf.stem
            job_prefix = "figure"
            job_naming = getattr(args, "naming", None) or "per-page"

        ui_utils.progress_bar(idx - 1, len(pdfs), prefix="Batch")
        ui_utils.info(
            f"[{idx}/{len(pdfs)}] {pdf.name} -> {job_output}"
        )

        # Build a per-iteration namespace copy.  Each job gets its own
        # ``CropConfig`` so we can override ``naming`` / ``output_prefix``
        # without polluting the others.
        from .config import CropConfig as _CC

        job_cfg = _CC.from_dict({**cfg.to_dict(), "output_prefix": job_prefix, "naming": job_naming})
        job_args = _build_job_args(args, pdf, job_output)
        try:
            rc = run_pipeline(job_args, job_cfg)
        except Exception as exc:  # pragma: no cover -- defensive top-level
            log.exception("Unhandled error on %s: %s", pdf, exc)
            rc = 1

        if rc == 0:
            result.succeeded += 1
            for hit in job_output.glob("*.pdf"):
                result.outputs.append(hit)
        else:
            result.failed += 1
            result.failures.append(pdf)

    ui_utils.progress_bar(len(pdfs), len(pdfs), prefix="Batch  ")
    print()  # newline after the final progress bar
    return result


def _build_job_args(args, pdf: Path, job_output: Path):
    """Construct a fresh ``argparse.Namespace`` mirroring ``args``."""
    import argparse

    job_args = argparse.Namespace(**vars(args))
    job_args.input = pdf
    job_args.output = job_output
    return job_args


__all__ = [
    "BatchResult",
    "collect_pdfs",
    "process_batch",
]