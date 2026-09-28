"""Tests for the batch-processing layer.

The tests exercise:

* :func:`pdf_auto_crop.batch.collect_pdfs` -- file / directory / glob inputs.
* :func:`pdf_auto_crop.batch.process_batch` -- end-to-end batch run on the
  real fixture PDFs, asserting the expected output structure and counts.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

import pytest

from pdf_auto_crop.batch import BatchResult, collect_pdfs, process_batch
from pdf_auto_crop.config import CropConfig


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


# ---------------------------------------------------------------------------
# collect_pdfs tests


def test_collect_pdfs_single_file():
    pdfs = collect_pdfs(FIXTURES / "case1_single_figure.pdf")
    assert len(pdfs) == 1
    assert pdfs[0].name == "case1_single_figure.pdf"


def test_collect_pdfs_directory():
    pdfs = collect_pdfs(FIXTURES)
    names = {p.name for p in pdfs}
    # Every fixture should appear at least once.
    for expected in (
        "case1_single_figure.pdf",
        "case2_four_figures.pdf",
        "case3_fem_mesh.pdf",
        "case4_wide_gap.pdf",
        "case5_close_gap.pdf",
        "case6_multipage.pdf",
        "case7_vector_only.pdf",
    ):
        assert expected in names


def test_collect_pdfs_directory_non_recursive(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.pdf").write_bytes(b"")
    (tmp_path / "top.pdf").write_bytes(b"")
    pdfs = collect_pdfs(tmp_path)
    names = {p.name for p in pdfs}
    assert "top.pdf" in names
    assert "inner.pdf" not in names


def test_collect_pdfs_directory_recursive(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.pdf").write_bytes(b"")
    (tmp_path / "top.pdf").write_bytes(b"")
    pdfs = collect_pdfs(tmp_path, recursive=True)
    names = {p.name for p in pdfs}
    assert "top.pdf" in names
    assert "inner.pdf" in names


def test_collect_pdfs_glob(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"")
    (tmp_path / "b.txt").write_bytes(b"")
    pdfs = collect_pdfs(str(tmp_path / "*.pdf"))
    names = {p.name for p in pdfs}
    assert names == {"a.pdf"}


def test_collect_pdfs_sequence_of_paths(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"")
    b.write_bytes(b"")
    pdfs = collect_pdfs([a, b])
    assert {p.name for p in pdfs} == {"a.pdf", "b.pdf"}


def test_collect_pdfs_unique(tmp_path):
    a = tmp_path / "a.pdf"
    a.write_bytes(b"")
    pdfs = collect_pdfs([a, a, a])
    assert len(pdfs) == 1


def test_collect_pdfs_sorted(tmp_path):
    for name in ("z.pdf", "a.pdf", "m.pdf"):
        (tmp_path / name).write_bytes(b"")
    pdfs = collect_pdfs(tmp_path)
    names = [p.name for p in pdfs]
    assert names == sorted(names)


def test_collect_pdfs_empty_dir(tmp_path):
    assert collect_pdfs(tmp_path) == []


# ---------------------------------------------------------------------------
# process_batch tests


EXPECTED_PER_FILE = {
    "case1_single_figure.pdf": 1,
    "case2_four_figures.pdf": 4,
    "case3_fem_mesh.pdf": 1,
    "case4_wide_gap.pdf": 2,
    "case5_close_gap.pdf": 2,
    "case6_multipage.pdf": 7,
    "case7_vector_only.pdf": 1,
}


def _make_args(output_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        input=None,
        output=output_dir,
        margin=None,
        threshold=None,
        dpi=None,
        min_area=None,
        min_width=None,
        min_height=None,
        morph_kernel_multiplier=None,
        merge_gap_mm=None,
        padding_mm=None,
        preview=False,
        preview_only=False,
        naming=None,
        output_prefix=None,
        log_level="WARNING",
        save_config=False,
        config=None,
        interactive=False,
        preset=None,
        batch_recursive=False,
        batch_flat=False,
    )


def test_batch_creates_output_directory(tmp_path):
    pdfs = collect_pdfs(FIXTURES)
    cfg = CropConfig()
    out = tmp_path / "batch_out"
    args = _make_args(out)
    result = process_batch(pdfs, out, cfg=cfg, args=args)
    assert out.exists()
    assert result.total == len(pdfs)


def test_batch_processes_all_fixtures(tmp_path):
    pdfs = collect_pdfs(FIXTURES)
    assert len(pdfs) == 7
    cfg = CropConfig()
    out = tmp_path / "batch_out"
    args = _make_args(out)
    result = process_batch(pdfs, out, cfg=cfg, args=args)

    assert result.total == 7
    assert result.succeeded == 7
    assert result.failed == 0

    # Each per-PDF subdirectory should contain the expected number of PDFs.
    for pdf in pdfs:
        sub = out / pdf.stem
        assert sub.exists(), f"missing subdir {sub}"
        produced = sorted(sub.glob("*.pdf"))
        assert len(produced) == EXPECTED_PER_FILE[pdf.name], (
            f"{pdf.name}: expected {EXPECTED_PER_FILE[pdf.name]} PDFs, "
            f"got {len(produced)}: {[p.name for p in produced]}"
        )


def test_batch_flat_layout(tmp_path):
    pdfs = collect_pdfs(FIXTURES)[:2]
    cfg = CropConfig()
    out = tmp_path / "flat_out"
    args = _make_args(out)
    result = process_batch(pdfs, out, cfg=cfg, args=args, flat_layout=True)
    assert result.succeeded == 2
    # With flat layout no per-file subdirs are created.
    for pdf in pdfs:
        assert not (out / pdf.stem).exists()
    # All PDFs are dropped directly under out/ with the per-file prefix.
    produced = list(out.glob("*.pdf"))
    assert len(produced) >= sum(EXPECTED_PER_FILE[p.name] for p in pdfs)


def test_batch_returns_batch_result(tmp_path):
    pdfs = collect_pdfs(FIXTURES)[:1]
    cfg = CropConfig()
    out = tmp_path / "single_out"
    args = _make_args(out)
    result = process_batch(pdfs, out, cfg=cfg, args=args)
    assert isinstance(result, BatchResult)
    assert result.total == 1
    assert result.succeeded == 1
    assert result.failed == 0


def test_batch_empty_input(tmp_path):
    cfg = CropConfig()
    out = tmp_path / "empty_out"
    args = _make_args(out)
    result = process_batch([], out, cfg=cfg, args=args)
    assert result.total == 0
    assert result.succeeded == 0
    assert result.outputs == []
    assert result.failures == []


def test_batch_skips_missing_files(tmp_path):
    cfg = CropConfig()
    out = tmp_path / "skip_out"
    args = _make_args(out)
    ghost = tmp_path / "ghost.pdf"
    result = process_batch([ghost], out, cfg=cfg, args=args)
    assert result.total == 1
    assert result.skipped == 1
    assert result.succeeded == 0