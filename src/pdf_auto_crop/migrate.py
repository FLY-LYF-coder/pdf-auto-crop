"""Rewriting the labels of an old export in the face the canvas shows.

The compositor used to write the labels with the PDF base-14 faces.  The
reader draws those with whatever Times it happens to have -- not the Times
New Roman the canvas showed, and not the Times New Roman a thesis is set
in.  This module takes such a file and writes the labels again in the face
the canvas draws with: the very file, embedded.

The labels are the **page's own** text.  The figures are Form XObjects, and
their text lives inside them, so a label can be taken away without touching
the artwork.  Each label of the old build sits in a content stream of its
own, and the safest way to remove it is to empty that stream; when a stream
holds more than the label, the label is removed with a redaction instead --
which is still only the page's own text that goes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pymupdf as fitz

from . import fonts

#: A text matrix that only moves the text.
_PLAIN_MATRIX = re.compile(rb"1\s+0\s+0\s+1\s+([-\d.]+)\s+([-\d.]+)\s+Tm")
#: The font and the size a text is written with.
_FONT = re.compile(rb"/([^\s/\[\]<>()]+)\s+([\d.]+)\s+Tf")
#: The colour a text is filled with.
_COLOUR = re.compile(rb"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg")
#: The text itself: a hex string in brackets, or a literal in parentheses.
_TEXT = re.compile(rb"\[<([0-9a-fA-F]*)>\]\s*TJ|\((.*?)\)\s*Tj")
#: The block a piece of text is written in.
_BLOCK = re.compile(rb"BT(.*?)ET", re.S)

#: The names the compositor wrote its labels with, and the face they mean.
#: These are the aliases of the built-in faces; the compositor always used
#: them, so a resource named like one of them is a label of ours.
ALIASES: Dict[str, str] = {
    "tiro": "Times-Roman",
    "tibo": "Times-Bold",
    "tiit": "Times-Italic",
    "tibiz": "Times-BoldItalic",
    "helv": "Helvetica",
    "hebo": "Helvetica-Bold",
    "hebi": "Helvetica-Oblique",
    "hebiz": "Helvetica-BoldOblique",
    "cour": "Courier",
    "cobo": "Courier-Bold",
    "coit": "Courier-Oblique",
    "cobi": "Courier-BoldOblique",
    "symb": "Symbol",
    "zadb": "ZapfDingbats",
}


@dataclass
class Label:
    """A piece of text the old build wrote on the page itself."""

    text: str
    #: The start of the baseline, in the page's top-down frame.
    origin: fitz.Point
    size: float
    color: Tuple[float, float, float]
    #: The built-in face it was written with, e.g. "Times-Bold".
    face: str
    #: The page it belongs to.
    page: int
    #: The content stream it lives in.
    stream: int
    #: Whether the stream holds nothing but this label.
    alone: bool = False

    def rect(self) -> fitz.Rect:
        """The box the text occupies, in the page's top-down frame."""
        width = fitz.get_text_length(self.text, fontname="tiro",
                                     fontsize=self.size)
        return fitz.Rect(self.origin.x, self.origin.y - self.size,
                         self.origin.x + width, self.origin.y + self.size * 0.3)


def _decode(data: bytes) -> str:
    """The text of a string in the WinAnsi encoding the built-in faces use."""
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _resources(page: fitz.Page) -> Dict[str, str]:
    """The base font of every resource the page names, by resource name."""
    out: Dict[str, str] = {}
    for font in page.get_fonts(full=True):
        # (xref, extension, type, basefont, resource name, encoding, ...)
        name = font[4] if len(font) > 4 else ""
        if name:
            out[name] = font[3]
    return out


def _only_label(stream: bytes) -> bool:
    """Whether a content stream holds nothing but one piece of text."""
    stripped = _BLOCK.sub(b"", stream)
    return not stripped.strip(b" \t\r\nqQ")


def _to_page_space(page: fitz.Page, x: float, y: float) -> fitz.Point:
    """A point of the page's user space, in the frame PyMuPDF counts in.

    The content is written in the user space of the **MediaBox**, counting
    from the bottom left.  PyMuPDF counts from the top left of the
    **CropBox**, which is what a cropped file has: the crop takes a part of
    the page out, and the content keeps the coordinates it always had.
    """
    media = page.mediabox
    crop = page.cropbox
    x_top = x - media.x0
    y_top = media.y1 - y
    return fitz.Point(x_top - crop.x0, y_top - crop.y0)


def find_labels(doc: fitz.Document) -> List[Label]:
    """The labels of the old build, in the order the pages hold them."""
    out: List[Label] = []
    for pno, page in enumerate(doc):
        resources = _resources(page)
        for xref in page.get_contents():
            try:
                data = doc.xref_stream(xref)
            except Exception:  # noqa: BLE001 -- no stream, no label
                continue
            blocks = _BLOCK.findall(data)
            if len(blocks) != 1:
                continue
            block = blocks[0]
            matrix = _PLAIN_MATRIX.search(block)
            font = _FONT.search(block)
            text_match = _TEXT.search(block)
            if not (matrix and font and text_match):
                continue
            resource = font.group(1).decode("latin-1")
            # The name the compositor wrote it with is the surest sign; the
            # page's own resources are the next best thing.
            face = ALIASES.get(resource) or resources.get(resource, "")
            # Only the Times faces are rewritten: they are the ones a thesis
            # set in Times New Roman has to agree with.  A page of another
            # tool's text is left alone.
            if face not in fonts.BASE14_TIMES:
                continue
            if text_match.group(1) is not None:
                text = _decode(bytes.fromhex(text_match.group(1).decode()))
            else:
                text = _decode(text_match.group(2) or b"")
            if not text.strip():
                continue
            colour = _COLOUR.search(block)
            rgb: Tuple[float, float, float] = (0.0, 0.0, 0.0)
            if colour:
                rgb = (float(colour.group(1)), float(colour.group(2)),
                       float(colour.group(3)))
            origin = _to_page_space(page, float(matrix.group(1)),
                                    float(matrix.group(2)))
            out.append(Label(
                text=text,
                origin=origin,
                size=float(font.group(2)),
                color=rgb,
                face=face,
                page=pno,
                stream=xref,
                alone=_only_label(data),
            ))
    return out


def _resource_of(page: fitz.Page, basefont: str) -> Optional[str]:
    """The resource name a page gives the built-in face, if it still has it."""
    for font in page.get_fonts(full=True):
        # (xref, extension, type, basefont, resource name, encoding, ...)
        if font[3] == basefont and len(font) > 4 and font[4]:
            return font[4]
    return None


def rewrite_labels(doc: fitz.Document) -> Tuple[int, List[str], Dict[str, str]]:
    """Write the labels of the old build again in the face the canvas shows.

    Returns:
        The number of labels that were rewritten, the ones that could not be
        (with the reason), and the text that was written, by the font it was
        written with.
    """
    labels = find_labels(doc)
    if not labels:
        return 0, [], {}
    emptied = set()
    written: Dict[str, str] = {}
    count = 0
    problems: List[str] = []
    for label in labels:
        path = fonts.file_for_base14(label.face)
        if not path:
            problems.append("no file for {} ({})".format(label.face, label.text))
            continue
        page = doc[label.page]
        if label.alone and label.stream not in emptied:
            # The stream holds nothing but this label: empty it.
            doc.update_stream(label.stream, b"")
            emptied.add(label.stream)
        else:
            page.add_redact_annot(label.rect(), fill=None)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        name = re.sub(r"[^A-Za-z0-9]", "", fitz.Font(fontfile=path).name)
        page.insert_text(
            label.origin, label.text, fontsize=label.size,
            fontname=name, fontfile=path, color=label.color,
        )
        written[name] = written.get(name, "") + label.text
        count += 1
    return count, problems, written


def migrate_file(src: Path, dst: Path) -> Tuple[int, List[str]]:
    """Write a copy of ``src`` whose labels carry the real font.

    Args:
        src: The file to rewrite.
        dst: Where the rewritten file goes.

    Returns:
        The number of labels that were rewritten, and the ones that could
        not be, with the reason.
    """
    from .compositor import repair_spaces, subset_fonts

    doc = fitz.open(str(src))
    try:
        count, problems, written = rewrite_labels(doc)
        if count:
            subset_fonts(doc)
            repair_spaces(doc, written)
            dst.parent.mkdir(parents=True, exist_ok=True)
            doc.save(str(dst), garbage=4, deflate=True)
    finally:
        doc.close()
    return count, problems


def migrate_tree(root: Path, suffix: str = "_tnr"
                 ) -> List[Tuple[Path, Path, int]]:
    """Rewrite every PDF under a directory, leaving the originals alone.

    Returns:
        One ``(source, written, labels)`` for every file that was rewritten.
    """
    out: List[Tuple[Path, Path, int]] = []
    for path in sorted(root.rglob("*.pdf")):
        if path.stem.endswith(suffix):
            continue
        dst = path.with_name(path.stem + suffix + ".pdf")
        count, _ = migrate_file(path, dst)
        if count:
            out.append((path, dst, count))
    return out


__all__ = [
    "Label",
    "find_labels",
    "migrate_file",
    "migrate_tree",
    "rewrite_labels",
]
