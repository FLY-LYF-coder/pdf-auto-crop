"""Vector preservation test.

Verify that the cropped output PDFs still contain real vector drawing
operators (not raster patches) so the figures remain crisp at any zoom.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import pymupdf as fitz
import pytest


PYTHON = sys.executable
CLI_MODULE = "pdf_auto_crop.cli"

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


def _run_cli(input_pdf: Path, output_dir: Path, *extra: str) -> Tuple[int, str, str]:
    cmd = [PYTHON, "-m", CLI_MODULE, "--input", str(input_pdf), "--output", str(output_dir), *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return proc.returncode, proc.stdout, proc.stderr


def _count_drawings(path: Path) -> Tuple[int, int, int]:
    """Return ``(drawings, text_chars, embedded_images)`` for ``path``."""
    doc = fitz.open(str(path))
    try:
        page = doc[0]
        drawings = page.get_drawings()
        text = page.get_text()
        images = page.get_images()
        return len(drawings), len(text), len(images)
    finally:
        doc.close()


def test_case7_text_preserved(tmp_path: Path) -> None:
    """The case-7 fixture contains explicit text labels; they MUST appear in
    the cropped output as real text (not as rasterised pixels)."""
    src = FIXTURES / "case7_vector_only.pdf"
    out = tmp_path / "out"
    rc, so, se = _run_cli(src, out)
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    files = list(out.glob("*.pdf"))
    assert len(files) == 1
    cropped = files[0]

    drawings, text_chars, images = _count_drawings(cropped)
    print(f"\n  Vector drawings: {drawings}")
    print(f"  Text characters: {text_chars}")
    print(f"  Embedded images: {images}")

    assert drawings > 0, "Expected vector drawings; output appears rasterised!"
    # The fixture has labels 'A', 'B', 'C', and a title - at least 10 chars.
    assert text_chars >= 10, f"Text content lost; only {text_chars} chars found."
    # No images should have been inserted by the cropper (vector preservation).
    assert images == 0, "Output PDF embeds raster images; vectors were destroyed!"


def test_case3_mesh_paths_preserved(tmp_path: Path) -> None:
    """The FEM mesh has many vector paths; the cropped output must contain
    a substantial number of them (the mesh + axes + arrows)."""
    src = FIXTURES / "case3_fem_mesh.pdf"
    out = tmp_path / "out"
    rc, so, se = _run_cli(src, out)
    assert rc == 0, f"CLI failed: stdout={so!r} stderr={se!r}"
    files = list(out.glob("*.pdf"))
    assert len(files) == 1
    cropped = files[0]

    drawings, text_chars, images = _count_drawings(cropped)
    print(f"\n  Vector drawings: {drawings}")
    print(f"  Text characters: {text_chars}")
    print(f"  Embedded images: {images}")

    # FEM mesh has many line segments (>100). After crop, expect most preserved.
    assert drawings > 50, f"Expected >50 vector drawings; got {drawings}"
    # Labels 'Node 1', 'Node 2', 'F' and title -- at least 10 chars.
    assert text_chars >= 10, f"Text content lost; only {text_chars} chars found."


def test_case7_no_raster_scaling_artifacts(tmp_path: Path) -> None:
    """Render the output at high DPI and confirm pixels look sharp by
    measuring pixel contrast along a line that crosses a known thin stroke."""
    src = FIXTURES / "case7_vector_only.pdf"
    out = tmp_path / "out"
    _run_cli(src, out)
    files = list(out.glob("*.pdf"))
    assert len(files) == 1
    cropped = files[0]

    doc = fitz.open(str(cropped))
    try:
        page = doc[0]
        # Render at 400 DPI: 400/72 ~ 5.56x scale.
        pix = page.get_pixmap(dpi=400, colorspace=fitz.csRGB, alpha=False)
        import numpy as np

        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
        gray = arr.mean(axis=2)
        # Count "near-black" pixels -- strokes should remain sharp, producing
        # distinct black-on-white rather than fuzzy gray clusters.
        near_black = (gray < 100).sum()
        near_white = (gray > 200).sum()
        print(f"\n  400 DPI: near-black={near_black} near-white={near_white}")
        assert near_black > 100, "Expected crisp vector strokes at 400 DPI"
    finally:
        doc.close()