"""Give the labels of an old export the face the canvas shows.

The compositor used to write the labels with the PDF base-14 faces.  The
reader draws those with whatever Times it has, which is not the Times New
Roman the canvas showed.  This script writes the labels again in the face
the canvas draws with -- the very file, embedded -- and leaves the original
alone.

    python scripts/migrate_label_font.py <file-or-folder> [...]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pdf_auto_crop import migrate  # noqa: E402

SUFFIX = "_tnr"


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]]
    if not targets:
        print(__doc__)
        print("give a PDF or a folder to rewrite")
        return 2

    total = 0
    for target in targets:
        if target.is_dir():
            results = migrate.migrate_tree(target, SUFFIX)
            for src, dst, count in results:
                print("  %-70s %2d label(s) -> %s"
                      % (str(src)[-70:], count, dst.name))
            total += len(results)
        elif target.is_file():
            dst = target.with_name(target.stem + SUFFIX + ".pdf")
            count, problems = migrate.migrate_file(target, dst)
            if count:
                print("  %-70s %2d label(s) -> %s"
                      % (str(target)[-70:], count, dst.name))
                total += 1
            for problem in problems:
                print("    ! %s" % problem)
        else:
            print("  not there: %s" % target)
    print("%d file(s) rewritten" % total)
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
