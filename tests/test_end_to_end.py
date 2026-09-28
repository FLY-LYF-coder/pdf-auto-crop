"""End-to-end tests for pdf-auto-crop.

Each test invokes the real CLI on a real fixture PDF and verifies:

* The process exits successfully.
* The expected number of cropped PDFs is produced.
* Each output PDF is non-trivial (>1 KB), opens with PyMuPDF, and has
  exactly 1 page (the tool emits single-figure PDFs).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import pymupdf as fitz
import pytest


# Always invoke the same interpreter pytest itself is running on.  Using
# ``shutil.which("python")`` would resolve to whatever happens to be first on
# PATH, which may not have ``pdf_auto_crop`` installed.
PYTHON = sys.executable
CLI_MODULE = "pdf_auto_crop.cli"

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
FIXTURES_DIAG = ROOT / "fixtures_diag"


def _run_cli(input_pdf: Path, output_dir: Path, *extra: str) -> Tuple[int, str, str]:
    cmd = [PYTHON, "-m", CLI_MODULE, "--input", str(input_pdf), "--output", str(output_dir), *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return proc.returncode, proc.stdout, proc.stderr


def _pdf_files(out: Path) -> List[Path]:
    return sorted(out.glob("*.pdf"))


def _assert_single_page_pdf(path: Path) -> None:
    assert path.stat().st_size > 1024, f"{path} is suspiciously small"
    doc = fitz.open(str(path))
    try:
        assert len(doc) == 1, f"{path} has {len(doc)} pages, expected 1"
    finally:
        doc.close()


# ---------------------------------------------------------------------------

def test_case1_single_figure(tmp_path: Path) -> None:
    src = FIXTURES / "case1_single_figure.pdf"
    out = tmp_path / "out"
    rc, so, se = _run_cli(src, out)
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    files = _pdf_files(out)
    assert len(files) == 1, f"Expected 1 PDF, got {len(files)}: {[f.name for f in files]}"
    _assert_single_page_pdf(files[0])


def test_case2_four_figures(tmp_path: Path) -> None:
    src = FIXTURES / "case2_four_figures.pdf"
    out = tmp_path / "out"
    rc, so, se = _run_cli(src, out)
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    files = _pdf_files(out)
    assert len(files) == 4, f"Expected 4 PDFs, got {len(files)}: {[f.name for f in files]}"
    for f in files:
        _assert_single_page_pdf(f)


def test_case3_fem_mesh(tmp_path: Path) -> None:
    src = FIXTURES / "case3_fem_mesh.pdf"
    out = tmp_path / "out"
    rc, so, se = _run_cli(src, out)
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    files = _pdf_files(out)
    assert len(files) == 1, f"Expected 1 PDF, got {len(files)}: {[f.name for f in files]}"
    _assert_single_page_pdf(files[0])


def test_case4_wide_gap(tmp_path: Path) -> None:
    src = FIXTURES / "case4_wide_gap.pdf"
    out = tmp_path / "out"
    rc, so, se = _run_cli(src, out)
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    files = _pdf_files(out)
    assert len(files) == 2, f"Expected 2 PDFs, got {len(files)}: {[f.name for f in files]}"
    for f in files:
        _assert_single_page_pdf(f)


def test_case5_close_gap(tmp_path: Path) -> None:
    src = FIXTURES / "case5_close_gap.pdf"
    out = tmp_path / "out"
    rc, so, se = _run_cli(src, out)
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    files = _pdf_files(out)
    # 3 mm is close: should still split because margin expansion plus merge_gap
    # of 1.5 mm leaves enough whitespace for two separate rectangles.
    assert len(files) == 2, f"Expected 2 PDFs, got {len(files)}: {[f.name for f in files]}"
    for f in files:
        _assert_single_page_pdf(f)


def test_case6_multipage(tmp_path: Path) -> None:
    src = FIXTURES / "case6_multipage.pdf"
    out = tmp_path / "out"
    rc, so, se = _run_cli(src, out)
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    files = _pdf_files(out)
    # 1 + 4 + 2 = 7 figures
    assert len(files) == 7, f"Expected 7 PDFs, got {len(files)}: {[f.name for f in files]}"
    for f in files:
        _assert_single_page_pdf(f)


def test_case7_vector_only(tmp_path: Path) -> None:
    src = FIXTURES / "case7_vector_only.pdf"
    out = tmp_path / "out"
    rc, so, se = _run_cli(src, out)
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    files = _pdf_files(out)
    assert len(files) == 1, f"Expected 1 PDF, got {len(files)}: {[f.name for f in files]}"
    _assert_single_page_pdf(files[0])


# ---------------------------------------------------------------------------
# CLI robustness checks.

def test_cli_missing_input(tmp_path: Path) -> None:
    rc, _, _ = _run_cli(tmp_path / "does_not_exist.pdf", tmp_path / "out")
    assert rc != 0


def test_cli_save_config(tmp_path: Path) -> None:
    src = FIXTURES / "case1_single_figure.pdf"
    out = tmp_path / "out"
    rc, _, se = _run_cli(src, out, "--save-config")
    assert rc == 0, se
    assert (out / "config.json").exists()
    import json

    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert "margin_mm" in cfg and "threshold" in cfg


def test_cli_load_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text('{"margin_mm": 8.0, "threshold": 230}', encoding="utf-8")
    src = FIXTURES / "case1_single_figure.pdf"
    out = tmp_path / "out"
    rc, _, se = _run_cli(src, out, "--config", str(cfg_path))
    assert rc == 0, se
    files = _pdf_files(out)
    assert len(files) == 1


# ---------------------------------------------------------------------------
# Diagnostic layouts, driven through the real CLI.
#
# These probe the layouts that motivated the layout-adaptive detector: mixed
# row/column arrangements, dense meshes, and figures separated by very
# different gaps on the same page.

DIAG_CASES = [
    ("D1_horizontal_3.pdf", 3, "1x3 row, 15 mm gaps"),
    ("D2_3plus1.pdf", 4, "3 small above 1 large"),
    ("D3_big_left_4right.pdf", 5, "1 large left + 2x2 grid right"),
    ("D4_fem_blank_interior.pdf", 1, "dense FEM mesh, 30x30 mm blank interior"),
    ("D5_topology.pdf", 1, "topology optimisation blob"),
    ("D6_tiny_gap.pdf", 2, "2 figures, 0.5 mm gap"),
    ("D7_mixed_gaps.pdf", 3, "3 figures, 20 mm + 1 mm gaps"),
    ("D8_mixed_widths.pdf", 3, "1 row, widths 30/60/30 mm"),
]


@pytest.mark.parametrize("name, expected, _desc", DIAG_CASES)
def test_diagnostic_layout(
    tmp_path: Path, name: str, expected: int, _desc: str
) -> None:
    """Each diagnostic fixture yields the expected number of single-page PDFs."""
    src = FIXTURES_DIAG / name
    out = tmp_path / "out"
    rc, so, se = _run_cli(src, out)
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    files = _pdf_files(out)
    assert len(files) == expected, (
        f"{name}: expected {expected} PDFs, got {len(files)}: "
        f"{[f.name for f in files]}"
    )
    for f in files:
        _assert_single_page_pdf(f)


def test_d7_legacy_cannot_but_smart_can(tmp_path: Path) -> None:
    """D7 is the regression that motivated the smart detector.

    Three figures are stacked with a 20 mm gap on top and a 1 mm gap at the
    bottom.  The legacy detector's single fixed merge gap either fuses the
    bottom pair or splits everything; only the adaptive one returns 3.
    """
    src = FIXTURES_DIAG / "D7_mixed_gaps.pdf"

    legacy_out = tmp_path / "legacy"
    rc, _, se = _run_cli(src, legacy_out, "--algorithm", "legacy")
    assert rc == 0, se
    legacy_count = len(_pdf_files(legacy_out))
    assert legacy_count == 2, f"legacy unexpectedly found {legacy_count}"

    smart_out = tmp_path / "smart"
    rc, _, se = _run_cli(src, smart_out, "--algorithm", "smart")
    assert rc == 0, se
    assert len(_pdf_files(smart_out)) == 3


def test_algorithm_defaults_to_smart(tmp_path: Path) -> None:
    """Omitting --algorithm must behave exactly like --algorithm smart."""
    src = FIXTURES_DIAG / "D7_mixed_gaps.pdf"
    default_out = tmp_path / "default"
    smart_out = tmp_path / "smart"

    rc, _, se = _run_cli(src, default_out)
    assert rc == 0, se
    rc, _, se = _run_cli(src, smart_out, "--algorithm", "smart")
    assert rc == 0, se

    assert [p.name for p in _pdf_files(default_out)] == [
        p.name for p in _pdf_files(smart_out)
    ]
    assert len(_pdf_files(default_out)) == 3


def test_algorithm_survives_config_file(tmp_path: Path) -> None:
    """``algorithm`` round-trips through the JSON config file."""
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text('{"algorithm": "legacy"}', encoding="utf-8")
    src = FIXTURES_DIAG / "D7_mixed_gaps.pdf"
    out = tmp_path / "out"
    rc, _, se = _run_cli(src, out, "--config", str(cfg_path))
    assert rc == 0, se
    assert len(_pdf_files(out)) == 2


def test_save_config_records_algorithm(tmp_path: Path) -> None:
    import json

    src = FIXTURES / "case1_single_figure.pdf"
    out = tmp_path / "out"
    rc, _, se = _run_cli(src, out, "--save-config", "--algorithm", "smart")
    assert rc == 0, se
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert cfg["algorithm"] == "smart"


def test_legacy_still_passes_original_cases(tmp_path: Path) -> None:
    """``--algorithm legacy`` keeps the original detector working unchanged."""
    src = FIXTURES / "case2_four_figures.pdf"
    out = tmp_path / "out"
    rc, _, se = _run_cli(src, out, "--algorithm", "legacy")
    assert rc == 0, se
    assert len(_pdf_files(out)) == 4


def test_cli_rejects_unknown_algorithm(tmp_path: Path) -> None:
    rc, _, _ = _run_cli(
        FIXTURES / "case1_single_figure.pdf", tmp_path / "out",
        "--algorithm", "telepathic",
    )
    assert rc != 0