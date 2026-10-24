"""Tests for the LaTeX mathematics renderer.

A formula has to come out as **vector** PDF with the font embedded, so it
stays sharp and its symbols stay selectable -- the same promise the rest of
the composition keeps.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
import pytest

from pdf_auto_crop import tex


pytestmark = pytest.mark.skipif(not tex.math_available(),
                                reason="matplotlib is not installed")


def test_a_formula_is_rendered_to_a_pdf() -> None:
    path = tex.render_math("E = mc^2", 12.0)
    assert path is not None
    assert Path(path).exists()
    doc = fitz.open(str(path))
    try:
        assert len(doc) == 1
        assert doc[0].rect.width > 0
    finally:
        doc.close()


def test_a_formula_is_vector_not_a_picture() -> None:
    path = tex.render_math(r"\frac{1}{2}\sum_{i=1}^{n} x_i", 12.0)
    assert path is not None
    doc = fitz.open(str(path))
    try:
        assert doc[0].get_images() == [], "the formula was rasterised"
        assert doc.get_page_fonts(0), "no font was embedded"
    finally:
        doc.close()


def test_the_symbols_are_readable() -> None:
    """Greek letters and operators have to survive the typesetting."""
    path = tex.render_math(r"\alpha + \beta", 12.0)
    assert path is not None
    doc = fitz.open(str(path))
    try:
        text = doc[0].get_text()
        assert "α" in text and "β" in text
    finally:
        doc.close()


def test_the_dollars_are_optional() -> None:
    """Without them the text would be typeset as ordinary letters."""
    plain = tex.render_math("x", 12.0)
    wrapped = tex.render_math("$x$", 12.0)
    assert plain is not None and wrapped is not None
    doc_a = fitz.open(str(plain))
    doc_b = fitz.open(str(wrapped))
    try:
        assert abs(doc_a[0].rect.width - doc_b[0].rect.width) < 0.5
    finally:
        doc_a.close()
        doc_b.close()


def test_the_size_follows_the_point_size() -> None:
    small = tex.render_math("x", 10.0)
    big = tex.render_math("x", 20.0)
    assert small is not None and big is not None
    doc_a = fitz.open(str(small))
    doc_b = fitz.open(str(big))
    try:
        assert doc_b[0].rect.height > doc_a[0].rect.height * 1.5
    finally:
        doc_a.close()
        doc_b.close()


def test_the_same_formula_is_rendered_only_once() -> None:
    first = tex.render_math("E = mc^2", 12.0)
    second = tex.render_math("E = mc^2", 12.0)
    assert first == second


def test_a_formula_that_cannot_be_typeset_is_refused() -> None:
    """A broken formula must not take the export down with it."""
    path = tex.render_math(r"\begin{notanenvironment}", 12.0)
    assert path is None or Path(path).exists()


def test_an_empty_formula_is_refused() -> None:
    assert tex.render_math("", 12.0) is None
    assert tex.render_math("   ", 12.0) is None
