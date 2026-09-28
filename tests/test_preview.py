"""Preview tests.

Verify the ``--preview`` and ``--preview-only`` modes produce non-empty PNG
overlays of detected bounding boxes for every fixture.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import pytest


PYTHON = sys.executable
CLI_MODULE = "pdf_auto_crop.cli"

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"

CASES: List[str] = [
    "case1_single_figure",
    "case2_four_figures",
    "case3_fem_mesh",
    "case4_wide_gap",
    "case5_close_gap",
    "case7_vector_only",
]


def _run_cli(input_pdf: Path, output_dir: Path, *extra: str) -> Tuple[int, str, str]:
    cmd = [PYTHON, "-m", CLI_MODULE, "--input", str(input_pdf), "--output", str(output_dir), *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return proc.returncode, proc.stdout, proc.stderr


def _pngs(out: Path) -> List[Path]:
    return sorted(out.glob("*.png"))


@pytest.mark.parametrize("case", CASES)
def test_preview_only_produces_png(case: str, tmp_path: Path) -> None:
    src = FIXTURES / f"{case}.pdf"
    out = tmp_path / "preview"
    rc, so, se = _run_cli(src, out, "--preview-only")
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    pngs = _pngs(out)
    assert len(pngs) >= 1, f"Expected >=1 PNG in {out}, got {pngs}"
    for p in pngs:
        assert p.stat().st_size > 0, f"{p.name} is empty"
    # Preview-only mode must NOT write cropped PDFs.
    assert list(out.glob("*.pdf")) == []


@pytest.mark.parametrize("case", CASES)
def test_preview_alongside_crops(case: str, tmp_path: Path) -> None:
    src = FIXTURES / f"{case}.pdf"
    out = tmp_path / "preview_and_crops"
    rc, so, se = _run_cli(src, out, "--preview")
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    pdfs = sorted(out.glob("*.pdf"))
    pngs = _pngs(out)
    assert len(pdfs) >= 1
    assert len(pngs) >= 1
    for p in pngs:
        assert p.stat().st_size > 0, f"{p.name} is empty"