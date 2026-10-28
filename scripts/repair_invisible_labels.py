"""Put the labels back into a sheet where they came out empty.

A font that cannot write a character puts .notdef in the file: the text is
there, but there is nothing to see, and the label is invisible.  The
characters are lost with the glyphs, so they have to be given again -- by
default the labels are numbered (a), (b), ..., in the order they were
written.

    python scripts/repair_invisible_labels.py <file-or-folder> [...]
    python scripts/repair_invisible_labels.py --labels "（a）（b）" sheet.pdf
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pdf_auto_crop import migrate  # noqa: E402
from pdf_auto_crop.compositor import repair_spaces, subset_fonts  # noqa: E402

SUFFIX = "_fixed"


def repair_file(src: Path, dst: Path,
                labels=None) -> int:
    """Write a copy of the file with the empty labels filled in."""
    doc = fitz.open(str(src))
    try:
        count, problems = migrate.repair_invisible_labels(doc, labels)
        if count:
            subset_fonts(doc)
            repair_spaces(doc, {})
            dst.parent.mkdir(parents=True, exist_ok=True)
            doc.save(str(dst), garbage=4, deflate=True)
    finally:
        doc.close()
    for problem in problems:
        print("    ! %s" % problem)
    return count


def main(argv: list[str]) -> int:
    args = argv[1:]
    labels = None
    if "--labels" in args:
        at = args.index("--labels")
        raw = args[at + 1] if at + 1 < len(args) else ""
        args = args[:at] + args[at + 2:]
        # the labels are separated by spaces, or by the brackets themselves
        labels = [part for part in raw.split() if part] or list(raw)
    targets = [Path(a) for a in args]
    if not targets:
        print(__doc__)
        return 2
    total = 0
    for target in targets:
        paths = (sorted(target.rglob("*.pdf")) if target.is_dir()
                 else [target])
        for path in paths:
            if path.stem.endswith(SUFFIX) or path.stem.endswith("_tnr"):
                continue
            dst = path.with_name(path.stem + SUFFIX + ".pdf")
            count = repair_file(path, dst, labels)
            if count:
                print("  %-64s %d label(s) -> %s"
                      % (str(path)[-64:], count, dst.name))
                total += 1
    print("%d file(s) repaired" % total)
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
