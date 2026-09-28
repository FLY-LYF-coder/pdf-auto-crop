"""Run the CLI on every fixture and print a summary table."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Resolved from this file so the script works in any checkout, not just the
# machine it was written on.
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
OUTPUT_ROOT = ROOT / "examples"
OUTPUT_ROOT.mkdir(exist_ok=True)

CASES = [
    "case1_single_figure",
    "case2_four_figures",
    "case3_fem_mesh",
    "case4_wide_gap",
    "case5_close_gap",
    "case6_multipage",
    "case7_vector_only",
]


def main() -> None:
    py = sys.executable
    header = f"{'Case':28s} {'Out PDFs':10s} {'Pages':6s} {'Output files'}"
    print(header)
    print("-" * 100)
    for case in CASES:
        src = FIXTURES / f"{case}.pdf"
        out_dir = OUTPUT_ROOT / f"{case}_out"
        out_dir.mkdir(exist_ok=True)
        # Clear previous outputs.
        for p in out_dir.iterdir():
            if p.is_file():
                p.unlink()
        proc = subprocess.run(
            [py, "-m", "pdf_auto_crop.cli", "--input", str(src), "--output", str(out_dir), "--preview"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        # Parse "Detected N figure(s) on page P" lines.
        detected = []
        for line in proc.stdout.splitlines() + proc.stderr.splitlines():
            if "Detected" in line:
                # "21:42:51 | INFO     | pdf_auto_crop.cli | Detected 4 figure(s) on page 1"
                parts = line.split("Detected")[-1].split("figure(s)")
                detected.append(int(parts[0].strip()))
        files = sorted(out_dir.glob("*.pdf"))
        pages_total = 0
        for f in files:
            import pymupdf as fitz

            d = fitz.open(str(f))
            pages_total += len(d)
            d.close()
        names = ", ".join(p.name for p in files)
        print(f"{case:28s} {len(files):<10d} {pages_total:<6d} {names}")


if __name__ == "__main__":
    main()