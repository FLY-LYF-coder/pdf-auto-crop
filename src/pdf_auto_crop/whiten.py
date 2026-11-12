"""Take the white background off a figure page, without rasterising it.

A figure that comes out of a plotting library usually carries its own
background: matplotlib paints the whole page white before it draws
anything.  That white is real ink in the content stream, so it hides
whatever the figure is placed on top of.

There is exactly one way to take ink away and keep the result vector:
the paint must not happen.  So the painting operator of a background
fill is rewritten -- ``f`` becomes ``n`` (end the path, paint nothing)
and ``B`` becomes ``S`` (stroke, but do not fill).  One byte per fill;
everything else in the stream is left byte for byte as it was.

Which fills count as background is decided from the geometry: a white
fill that covers a good part of the figure is a background, a white
legend box a few per cent big is not.

The whole of a figure's content stream is never parsed.  A page can
carry half a gigabyte of vector strokes, but the background is always
drawn first, so only the head of the stream is looked at.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import fitz

#: A colour this close to white counts as white.
WHITE_LEVEL = 0.99

#: A white fill has to cover this much of the figure to be a background.
MIN_BACKGROUND_AREA = 0.10

#: How much of the content stream is looked at, in bytes.
SCAN_WINDOW = 65536

#: A stream bigger than this is left alone: rewriting it costs more time
#: and memory than the result is worth.
MAX_STREAM_BYTES = 256 * 1024 * 1024


# The content stream's tokens.  A literal string is matched first so that
# an ``f`` inside a string is never mistaken for an operator.
_TOKEN_RE = re.compile(
    rb"\((?:\\.|[^()\\])*\)"        # (literal string)
    rb"|<<|>>|\[|\]"                # dictionary / array brackets
    rb"|/[^\s/\[\]<>(){}%]*"        # /Name
    rb"|[-+]?(?:\d+\.?\d*|\.\d+)"   # number
    rb"|[A-Za-z][A-Za-z0-9*']*"     # operator
    rb"|\S",                        # anything else
    re.S)

#: The operators that paint a path.
_FILL_OPS = frozenset({"f", "F", "f*", "B", "B*", "b", "b*"})
#: The operators that end a path without painting it.
_NO_PAINT_OPS = frozenset({"n"})
#: The operators that end a path by painting its edge.
_STROKE_OPS = frozenset({"S", "s"})
#: The operator that closes the current subpath.
_CLOSE_OPS = frozenset({"h"})


@dataclass
class WhiteFill:
    """A white fill that covers enough of the figure to be a background."""

    #: The painting operator as it is written in the stream.
    op: str
    #: Where the operator sits in the stream.
    offset: int
    #: How much of the figure the fill covers, 0..1.
    frac: float
    #: The fill's bounding box, in the package's frame.
    bbox: Tuple[float, float, float, float]


@dataclass
class StripResult:
    """What the strip did."""

    #: How many fills were taken out.
    removed: int = 0
    #: Why nothing was done, when nothing was.
    note: str = ""


def _is_number(token: bytes) -> bool:
    return token[:1] in b"-+.0123456789"


def _numbers(operands: Sequence[object]) -> List[float]:
    return [v for v in operands if isinstance(v, float)]


def _names(operands: Sequence[object]) -> List[str]:
    return [v[1] for v in operands if isinstance(v, tuple)]


class _Scanner:
    """Walks the head of a content stream and watches the fills."""

    def __init__(self, data: bytes, white_level: float = WHITE_LEVEL) -> None:
        self.data = data
        self.white_level = white_level
        self.fill = (0.0, 0.0, 0.0)
        self.fill_known = True
        self.cs = "DeviceGray"
        self.stack: List[Tuple[Tuple[float, float, float], bool, str,
                               Tuple[float, float, float, float, float, float]]] = []
        self.ctm: Tuple[float, float, float, float, float, float] = (
            1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        self.path: List[Tuple[float, float]] = []
        self.path_ctm: Optional[Tuple[float, float, float, float, float, float]] = None
        self.fills: List[WhiteFill] = []

    # -- graphics state ------------------------------------------------
    @staticmethod
    def _multiply(m, n):
        a1, b1, c1, d1, e1, f1 = m
        a2, b2, c2, d2, e2, f2 = n
        return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
                c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
                e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)

    @staticmethod
    def _apply(m, x, y):
        a, b, c, d, e, f = m
        return (a * x + c * y + e, b * x + d * y + f)

    def _is_white(self) -> bool:
        return self.fill_known and all(v >= self.white_level for v in self.fill)

    def _bbox(self) -> Optional[Tuple[float, float, float, float]]:
        """The path's box, in the frame the content is drawn in."""
        if not self.path or self.path_ctm is None:
            return None
        pts = [self._apply(self.path_ctm, x, y) for x, y in self.path]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    # -- the walk ------------------------------------------------------
    def run(self) -> List[WhiteFill]:
        data = self.data
        operands: List[object] = []
        pos = 0
        for m in _TOKEN_RE.finditer(data):
            token = m.group()
            if _is_number(token):
                try:
                    operands.append(float(token))
                except ValueError:
                    operands.append(0.0)
                continue
            if token[:1] == b"/":
                operands.append(("name", token[1:].decode("latin-1")))
                continue
            if token[:1] == b"(" or token in (b"<<", b">>", b"[", b"]"):
                operands.append(token)
                continue

            op = token.decode("latin-1")
            if op == "q":
                self.stack.append((self.fill, self.fill_known, self.cs, self.ctm))
            elif op == "Q":
                if self.stack:
                    self.fill, self.fill_known, self.cs, self.ctm = self.stack.pop()
            elif op == "cm":
                nums = _numbers(operands)[-6:]
                if len(nums) == 6:
                    self.ctm = self._multiply(self.ctm, tuple(nums))
            elif op == "cs":
                names = _names(operands)
                self.cs = names[-1] if names else "DeviceGray"
                self.fill_known = False
            elif op == "g":
                nums = _numbers(operands)
                if nums:
                    self.fill = (nums[-1], nums[-1], nums[-1])
                    self.fill_known = True
            elif op == "rg":
                nums = _numbers(operands)
                if len(nums) >= 3:
                    self.fill = (nums[-3], nums[-2], nums[-1])
                    self.fill_known = True
            elif op == "k":
                nums = _numbers(operands)
                if len(nums) >= 4:
                    c, m_, y_, k_ = nums[-4:]
                    self.fill = ((1 - c) * (1 - k_), (1 - m_) * (1 - k_),
                                 (1 - y_) * (1 - k_))
                    self.fill_known = True
            elif op in ("sc", "scn"):
                self._set_colour(operands)
            elif op == "re":
                nums = _numbers(operands)[-4:]
                if len(nums) == 4:
                    self._begin_path()
                    x, y, w, h = nums
                    self.path += [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            elif op in ("m", "l"):
                nums = _numbers(operands)[-2:]
                if len(nums) == 2:
                    self._begin_path()
                    self.path.append((nums[0], nums[1]))
            elif op == "c":
                nums = _numbers(operands)[-6:]
                if len(nums) == 6:
                    self._begin_path()
                    self.path += [(nums[0], nums[1]), (nums[2], nums[3]),
                                  (nums[4], nums[5])]
            elif op in ("v", "y"):
                nums = _numbers(operands)[-4:]
                if len(nums) == 4:
                    self._begin_path()
                    self.path += [(nums[0], nums[1]), (nums[2], nums[3])]
            elif op == "h":
                pass  # closing a subpath does not change its box
            elif op in _FILL_OPS:
                self._painted(op, m.start())
                self._end_path()
            elif op in _STROKE_OPS or op in _NO_PAINT_OPS:
                self._end_path()
            elif op == "ID":
                # An inline image carries binary data; skip over it.
                pos = self._skip_inline_image(m.end())
                operands = []
                continue
            operands = []
        return self.fills

    def _begin_path(self) -> None:
        if not self.path:
            # A path takes the shape it had when it was begun; a ``cm``
            # after that does not move it.
            self.path_ctm = self.ctm

    def _end_path(self) -> None:
        self.path = []
        self.path_ctm = None

    def _set_colour(self, operands: Sequence[object]) -> None:
        """Read a colour set with ``sc``/``scn``."""
        if _names(operands):
            # A pattern, not a plain colour.
            self.fill_known = False
            return
        nums = _numbers(operands)
        if self.cs == "DeviceGray" and nums:
            self.fill = (nums[-1], nums[-1], nums[-1])
            self.fill_known = True
        elif self.cs == "DeviceRGB" and len(nums) >= 3:
            self.fill = (nums[-3], nums[-2], nums[-1])
            self.fill_known = True
        elif self.cs == "DeviceCMYK" and len(nums) >= 4:
            c, m_, y_, k_ = nums[-4:]
            self.fill = ((1 - c) * (1 - k_), (1 - m_) * (1 - k_),
                         (1 - y_) * (1 - k_))
            self.fill_known = True
        else:
            self.fill_known = False

    def _skip_inline_image(self, start: int) -> int:
        """Where the inline image that starts at ``start`` ends."""
        data = self.data
        at = start
        while True:
            found = data.find(b"EI", at)
            if found == -1:
                return len(data)
            before = data[found - 1:found]
            after = data[found + 2:found + 3]
            if (before in (b" ", b"\n", b"\r", b"\t", b"\x00", b"")
                    and after in (b" ", b"\n", b"\r", b"\t", b"/", b"[", b"]",
                                  b"<", b"(", b"", b"Q", b"q")):
                return found + 2
            at = found + 2

    def _painted(self, op: str, offset: int) -> None:
        if not self._is_white():
            return
        bbox = self._bbox()
        if bbox is None:
            return
        self.fills.append(WhiteFill(op=op, offset=offset, frac=0.0, bbox=bbox))


def frame_offset(page: fitz.Page) -> Tuple[float, float]:
    """Where the content's origin sits in the package's frame.

    A region is measured from the visible frame's bottom-left corner,
    while the content of a page is addressed in the page's own
    coordinates.  The two meet at the crop box's corner: the crop box is
    given in the same space the content is drawn in, and the visible
    frame starts at its lower-left corner.
    """
    crop = fitz.Rect(page.cropbox)
    return (crop.x0, crop.y0)


def scan_white_fills(
    page: fitz.Page,
    data: bytes,
    region: Optional[fitz.Rect] = None,
    *,
    white_level: float = WHITE_LEVEL,
    min_area: float = MIN_BACKGROUND_AREA,
    window: int = SCAN_WINDOW,
) -> List[WhiteFill]:
    """The white fills in the head of ``data`` that count as background.

    ``region`` is the area of the figure that will be placed, in the
    package's frame.  A fill has to cover that much of it to count.
    """
    scanner = _Scanner(data[:window], white_level=white_level)
    fills = scanner.run()
    if region is None:
        region = fitz.Rect(page.rect)
    ox, oy = frame_offset(page)
    target = fitz.Rect(region.x0 + ox, region.y0 + oy,
                       region.x1 + ox, region.y1 + oy)
    area = max(target.width, 1e-6) * max(target.height, 1e-6)
    out: List[WhiteFill] = []
    for fill in fills:
        box = fitz.Rect(fill.bbox)
        covered = box & target
        frac = max(covered.width, 0.0) * max(covered.height, 0.0) / area
        if frac < min_area:
            continue
        out.append(WhiteFill(op=fill.op, offset=fill.offset, frac=frac,
                              bbox=(box.x0 - ox, box.y0 - oy,
                                    box.x1 - ox, box.y1 - oy)))
    return out


def patch_fills(data: bytes, fills: Sequence[WhiteFill]) -> bytes:
    """Rewrite the fills so that they do not paint."""
    buf = bytearray(data)
    for fill in fills:
        op = fill.op
        if op in ("f", "F", "f*"):
            new = b"n"
        elif op in ("B", "B*"):
            new = b"S"
        else:
            new = b"s"
        old = op.encode("latin-1")
        if buf[fill.offset:fill.offset + len(old)] != old:
            continue
        # The stream keeps its length: a one-byte operator takes one
        # byte, a two-byte one takes a byte and a space.
        buf[fill.offset:fill.offset + len(old)] = new + b" " * (len(old) - 1)
    return bytes(buf)


def _deflate(data: bytes) -> bytes:
    """Compress the way the writer wants it: fast for a big stream."""
    level = 6 if len(data) < 8 * 1024 * 1024 else 1
    return zlib.compress(data, level)


def strip_page_background(
    doc: fitz.Document,
    page: fitz.Page,
    region: Optional[fitz.Rect] = None,
    *,
    white_level: float = WHITE_LEVEL,
    min_area: float = MIN_BACKGROUND_AREA,
    window: int = SCAN_WINDOW,
    max_bytes: int = MAX_STREAM_BYTES,
) -> StripResult:
    """Take the white background off ``page``, in place.

    The page's content streams are read, the background fills in their
    head are found, and the ones that cover enough of ``region`` stop
    painting.  Nothing else in the page is touched.
    """
    xrefs = list(page.get_contents())
    if not xrefs:
        return StripResult(0, "这一页没有内容流。")
    parts: List[bytes] = []
    for xref in xrefs:
        try:
            parts.append(doc.xref_stream(xref))
        except Exception:  # noqa: BLE001 -- a stream we cannot read
            return StripResult(0, "这一页的内容流读不出来。")
    total = sum(len(p) for p in parts)
    if total > max_bytes:
        return StripResult(0, f"内容流有 {total // (1024 * 1024)} MB，太大，没有处理。")

    full = b"".join(parts)
    fills = scan_white_fills(page, full, region, white_level=white_level,
                             min_area=min_area, window=window)
    if not fills:
        return StripResult(0, "没有找到白底。")

    # Hand every fill back to the stream it belongs to.
    bounds: List[Tuple[int, int]] = []
    at = 0
    for part in parts:
        bounds.append((at, at + len(part)))
        at += len(part)
    per_stream: List[List[WhiteFill]] = [[] for _ in parts]
    for fill in fills:
        for index, (start, end) in enumerate(bounds):
            if start <= fill.offset < end:
                local = WhiteFill(op=fill.op, offset=fill.offset - start,
                                  frac=fill.frac, bbox=fill.bbox)
                per_stream[index].append(local)
                break

    for index, xref in enumerate(xrefs):
        local = per_stream[index]
        if not local:
            continue
        patched = patch_fills(parts[index], local)
        if patched == parts[index]:
            continue
        doc.update_stream(xref, _deflate(patched), compress=False)
        doc.xref_set_key(xref, "Filter", "/FlateDecode")
    return StripResult(len(fills), "")
