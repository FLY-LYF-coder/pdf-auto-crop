# pdf-auto-crop

> **Automatically split a multi-figure PDF into single-figure PDFs — while
> preserving every vector path, glyph, and stroke.**

Built for the SCI-paper workflow where PowerPoint exports a single PDF page
containing several figures. `pdf-auto-crop` reads the page, finds the bounding
box of every figure, and emits one PDF per figure — using the PDF **CropBox**
mechanism rather than rasterisation, so the output stays sharp at any zoom
level.

```
+----------------------+         +--------+ +--------+ +--------+ +--------+
|                      |         |        | |        | |        | |        |
|   [ Fig 1 ] [ Fig 2 ]|  --->   | Fig 1  | | Fig 2  | | Fig 3  | | Fig 4  |
|                      |         |        | |        | |        | |        |
|   [ Fig 3 ] [ Fig 4 ]|         +--------+ +--------+ +--------+ +--------+
|                      |
+----------------------+
```

---

## Why pdf-auto-crop?

Most "PDF → image cropping" tools re-render the cropped region as a PNG/JPG
and embed that image into a new PDF. That destroys vector quality: text and
lines become pixel mush at 300% zoom.

`pdf-auto-crop` uses PyMuPDF's `Page.set_cropbox()` instead. The cropped
output is a brand-new PDF that **still references the original page's
drawing operators** — paths, text, fills, even embedded fonts are all
preserved.

| Approach | Quality at 400 % zoom | File size |
| -------- | --------------------- | --------- |
| Crop + re-rasterise | Fuzzy pixels | Small |
| `pdf-auto-crop` (CropBox) | Crisp vectors, real text | Small-medium |

---

## Installation

### pip (recommended)

```bash
pip install pymupdf opencv-python numpy pillow
pip install -e .            # from the cloned repository
```

### conda

```bash
conda install -c conda-forge pymupdf opencv numpy pillow
pip install -e .
```

> **Requires Python ≥ 3.9.** OpenCV is used only for image processing; the
> final output is pure PDF.

---

## Running the tests

The test PDFs are generated, not committed, so generate them once after
cloning:

```bash
pip install -e . pytest
python scripts/make_test_pdfs.py
pytest tests/ -q
```

The suite is 263 tests.  Two of them deserve a note, because they encode bugs
that a coordinate comparison cannot see:

- `test_crop_lands_on_the_requested_region` puts a marker of a unique colour in
  each of the four page corners, crops around one, and rasterises the result to
  confirm the right colour came back.  A vertical flip — the failure mode of a
  PyMuPDF/PDF coordinate-convention mix-up — is invisible to any test that
  compares numbers in a single convention.
- `test_shown_boxes_equal_the_exported_cropbox` asserts that what the GUI draws
  is what lands in the file, including the white border.

The GUI itself is exercised end to end by
`python scripts/gui_edit_screenshot.py`, which drives the real window with
synthetic mouse events and writes screenshots to `diag_out/`.  Both that
directory and the generated fixtures are git-ignored.

---

## Quick start

```bash
# Graphical interface: preview the split, fix any page by hand, then export
python -m pdf_auto_crop.gui

# Split input.pdf into ./output/figure_p01_f01.pdf, figure_p01_f02.pdf, ...
python -m pdf_auto_crop.cli --input input.pdf

# Custom output directory + larger margin:
python -m pdf_auto_crop.cli --input input.pdf --output ./figs --margin 5

# Generate a preview PNG without writing cropped PDFs:
python -m pdf_auto_crop.cli --input input.pdf --preview-only

# Use a tuned preset for tight figure layouts:
python -m pdf_auto_crop.cli --input input.pdf --preset tight

# Use the original fixed-parameter detector instead of the adaptive default:
python -m pdf_auto_crop.cli --input input.pdf --algorithm legacy

# Interactive wizard (default when no arguments are given):
python -m pdf_auto_crop.cli
# or, to choose between the window and the wizard:
python -m pdf_auto_crop
```

Sample session:

```text
$ python -m pdf_auto_crop.cli --input case2_four_figures.pdf
21:50:01 | INFO | pdf_auto_crop.pipeline | Processing case2_four_figures.pdf (algorithm=smart dpi=250 threshold=245 margin=3.0mm merge_gap=1.5mm)
21:50:01 | INFO | pdf_auto_crop.pipeline | --- Page 1/1 ---
21:50:02 | INFO | pdf_auto_crop.pipeline | Detected 4 figure(s) on page 1
21:50:02 | INFO | pdf_auto_crop.pipeline | Saved figure_p01_f01.pdf (216.3x244.5 pt)
21:50:02 | INFO | pdf_auto_crop.pipeline | Saved figure_p01_f02.pdf (216.3x244.5 pt)
21:50:02 | INFO | pdf_auto_crop.cli | Saved figure_p01_f03.pdf (198.4x226.8 pt)
21:50:02 | INFO | pdf_auto_crop.cli | Saved figure_p01_f04.pdf (198.4x226.8 pt)
21:50:02 | INFO | pdf_auto_crop.cli | Done. 4 figure(s) saved across 1 page(s); per-page counts=[4]
```

---

## Interactive wizard

When invoked without any arguments, `pdf-auto-crop` enters a four-step
interactive wizard. The wizard scans the current directory for PDFs,
prompts for a preset and output directory, and (optionally) lets the user
adjust parameters before committing:

```text
$ python -m pdf_auto_crop
============================================================
  PDF Auto Crop - Interactive Wizard
============================================================

-> Step 1/4: Select input PDF
[INFO] Found 3 PDF file(s) in /path/to/dir:
  [1] FILE case2_four_figures.pdf
  [2] FILE case3_fem_mesh.pdf
  [3] FILE my_paper.pdf
Choice (number, path, or 'q' to quit) [1]:

-> Step 2/4: Select preset
Which preset matches your layout?
  [ 1] tight     - compact cropping, ideal for tight multi-figure layouts
  [*2] standard  - balanced defaults
  [ 3] loose     - generous margin / merge gap
Choice [2]:

-> Step 3/4: Output directory
  Default: /path/to/dir/case2_four_figures.pdf_out
Press Enter to accept, or enter a custom path:

-> Step 4/4: Settings summary
  Input:     /path/to/dir/case2_four_figures.pdf
  Preset:    standard (margin=3.0mm, threshold=245, merge_gap=1.5mm)
  Output:    /path/to/dir/case2_four_figures.pdf_out
  Mode:      single

[Y] Confirm and proceed  /  [a] Adjust parameters  /  [q] Quit [y]:
```

The wizard remembers your last input/output directory and preset in
`~/.pdf_auto_crop.json` and pre-fills them on the next run.

---

## Graphical interface

```bash
python -m pdf_auto_crop          # offers GUI vs wizard
python -m pdf_auto_crop.gui      # straight to the window
```

The window renders each page and draws the detected figures on top of it, so
you see the split *before* anything is written to disk.  A feedback panel
explains what the detector found and offers a one-click fix where a safe one
exists.

### Automatic **and** manual crop windows, per page

The detector is fast and right most of the time.  When it is wrong, you can fix
it by hand — and you only have to fix the pages that are wrong.

Switch the toolbar to **手动框选** (manual) and drag on the page:

| Gesture | Effect |
| ------- | ------ |
| Drag on empty space | Draw a new crop box |
| Drag a box body | Move the box |
| Drag an orange corner handle | Resize from that corner |
| Right-click a box / `Delete` | Remove it |
| Arrow keys | Nudge 1 pt; hold `Shift` for 10 pt |
| `Esc` | Deselect |

**Blue** boxes are yours and will be exported exactly as drawn.
**Red** boxes are the detector's proposal for a page you have not touched.

So on a 40-page paper you redraw the two pages the algorithm split wrongly and
leave the other 38 alone.  `恢复自动检测` hands one page back to the detector,
and `清空所有手动修改` discards every manual edit.

### The frame you see is the frame you get

The two boxes on an automatic page mean different things, and both are drawn:

- **Solid frame** = the actual crop window written to the PDF, *including* the
  `白边` white border.
- **Dashed grey frame** = the image content the detector found, before the
  border.

Editing `白边` moves the solid frame immediately — detection does not re-run,
because the border does not affect it.

**Blue (manual) boxes are exported verbatim.**  The white border is *not*
re-applied on top of a hand-drawn rectangle: that is the crop window you chose.
Taking a page over carries the border into the first manual box, so switching
modes never resizes anything until you drag something yourself.

The output is written with `set_cropbox()` in both modes, so vector content is
preserved exactly.

> Manual editing is per-file.  In batch mode (a folder of PDFs) there is no
> single page to draw on, so the export is purely automatic.

---

## Presets

Three named presets cover most paper layouts.  The last two columns only take
effect under `--algorithm legacy`; under the default `smart` algorithm the
split is measured from the page, so presets effectively adjust `margin_mm` and
`threshold`.

| Preset      | `margin_mm` | `threshold` | `merge_gap_mm` | `morph_kernel_multiplier` | Use when |
| ----------- | ----------- | ----------- | -------------- | ------------------------- | -------- |
| `tight`     | 2.0         | 248         | 1.0            | 1.2                       | Dense 2×2 multi-figure layouts, sub-figures packed tightly |
| `standard`  | 3.0         | 245         | 1.5            | 1.0                       | Default; balanced for most layouts |
| `loose`     | 5.0         | 240         | 2.5            | 0.8                       | Single large figures; avoids false splits on sparse content |

```bash
python -m pdf_auto_crop.cli --input paper.pdf --preset tight
```

You can still pass `--margin`, `--threshold`, etc. on top of a preset --
the CLI overrides take precedence.

---

## Batch processing

Point `--input` at a directory (or glob) and every PDF inside is
processed in turn.  Each PDF gets its own subdirectory by default:

```bash
# Process every PDF in ./pdfs/ -> ./figures/<stem>/figure_p*.pdf
python -m pdf_auto_crop.cli --input ./pdfs/ --output ./figures/

# Glob pattern across an entire tree
python -m pdf_auto_crop.cli --input "./pdfs/*.pdf" --output ./figures/

# Flat layout: all outputs go directly into --output with a per-file prefix
python -m pdf_auto_crop.cli --input ./pdfs/ --output ./figures/ --batch-flat

# Recurse into subdirectories
python -m pdf_auto_crop.cli --input ./pdfs/ --batch-recursive
```

A progress bar tracks the batch:

```text
[INFO] Batch: 7 PDF(s) -> ./figures
[INFO] [1/7] paper_01.pdf -> ./figures/paper_01
[INFO] [2/7] paper_02.pdf -> ./figures/paper_02
...
Batch   |████████████████████████████████████████| 7/7 (100%)

============================================================
  Batch summary
============================================================

  Total:     7
  Succeeded: 7
  Failed:    0
  Skipped:   0
```

---

## Interactive preview-confirm

For a single PDF, the `--interactive` flag stops after each page to ask
whether the detected figures look correct:

```bash
python -m pdf_auto_crop.cli --input paper.pdf --interactive
```

```text
[INFO] Page 1: detected 4 figure(s).
Detection looks correct? [Y] crop  /  [n] skip  /  [q] quit [y]:
```

Reply `y` to crop, `n` to skip that page, `q` to abort (the preview
files remain on disk for inspection).

---

## Algorithm

Two detectors share one interface and one output format.  `--algorithm smart`
is the default; `--algorithm legacy` restores the original fixed-parameter
detector for layouts where you want predictable, manually-tuned behaviour.

### `smart` (default) — layout-adaptive

A fixed closing kernel and merge gap cannot serve both a 2×2 panel grid with
1 mm gutters and a single figure with a 20 mm margin: whichever value you pick,
one of the two splits wrong.  So the smart detector measures each page instead
of trusting a constant.

1. **Render to bitmap.**  Each page is rasterised at `--dpi` (default 250) into
   an RGB array.
2. **Binarise.**  Pixels brighter than `--threshold` are background (white);
   everything else is foreground.  This produces an ink mask.
3. **Sweep the inter-component gaps.**  Every pair of components that share at
   least 25 % of their extent on one axis contributes a gap measurement.  Pairs
   that merely clip by a pixel or two are excluded, so they cannot drag the
   minimum down.
4. **Look for two modes.**  The gaps are binned into a histogram and scored for
   bimodality.  A page with a 1 mm gutter and a 20 mm margin produces two clear
   peaks with a valley between them; a page with one figure produces one peak.
5. **Choose a closing kernel from a ladder.**  Bimodality picks the rung.
   The ladder runs 0.10 – 1.50 mm and includes an **identity (1×1) rung**, so
   that a page whose figures are separated by genuine whitespace can be left
   alone instead of having that whitespace closed away.
6. **Absorb contained components.**  A box strictly inside another is merged
   into it by union-find.  This resolves "a caption inside a plot frame"
   geometrically, without growing the kernel and risking the gutter in step 5.
7. **Merge.**  Components are unioned when their gap falls below
   `min_gap − 1`, i.e. only when they are genuinely touching.  The detected
   valley is used *only* if it falls below that safe bound.
8. **Filter by area / size.**  Discard components smaller than
   `--min-area`, `--min-width`, `--min-height`.
9. **Refine the bounding box.**  Re-snap each surviving blob onto the raw
   (un-closed) ink mask so dilation artefacts do not pad the crop.
10. **Convert to PDF coordinates** (origin = bottom-left, units = pt) and
    **sort** top-down, left-to-right.

Because step 5 and step 7 are chosen from the page's own measurements,
`--merge-gap-mm` and `--morph-kernel-multiplier` have no effect under `smart`.
That is why the GUI hides them rather than showing dead controls.

The `--margin` / `--padding-mm` border is still applied to every detected box,
in the GUI and on the command line alike.

### `legacy` — fixed parameters

The original pipeline, unchanged: binarise, close with
`round(dpi / 25 × --morph-kernel-multiplier)`, take connected components, merge
anything within `--merge-gap-mm`, filter by area/size, re-snap, convert, sort.
Use it when you want `--merge-gap-mm` and `--morph-kernel-multiplier` to mean
something, and are prepared to tune them per layout.

### Emit

For each figure, copy the source page into a new PDF and `set_cropbox()` to the
figure's rect — **vector data is preserved**.  The rect is in PDF user space
(bottom-left origin); see [Coordinate convention](#coordinate-convention) for
the one conversion that boundary requires.

---

## Parameters

| Flag | Type | Default | Description |
| ---- | ---- | ------- | ----------- |
| `--input` | path | wizard | Source PDF, directory of PDFs, or glob pattern.  When omitted, the interactive wizard starts.  (Running `python -m pdf_auto_crop` instead offers the GUI or the wizard.) |
| `--output` | path | `./output` | Output directory. |
| `--preset` | choice | `standard` | Apply a named preset: `tight`, `standard`, or `loose`. |
| `--interactive` | flag | off | Preview-confirm-execute mode for single-PDF processing. |
| `--batch-recursive` | flag | off | When `--input` is a directory, recurse into subdirectories. |
| `--batch-flat` | flag | off | Drop all batch outputs directly into `--output` with a per-file prefix (no per-PDF subdirectories). |
| `--margin` | float | 3.0 | White margin (mm) added around each figure. |
| `--threshold` | int | 245 | Binarisation threshold (0–255). |
| `--dpi` | int | 250 | Render DPI for analysis. |
| `--min-area` | float | 25 | Min figure area (mm²). |
| `--min-width` | float | 10 | Min figure width (mm). |
| `--min-height` | float | 10 | Min figure height (mm). |
| `--morph-kernel-multiplier` | float | 1.0 | Scale the close kernel. **`legacy` only.** |
| `--merge-gap-mm` | float | 1.5 | Max gap between blobs to merge. **`legacy` only.** |
| `--padding-mm` | float | 0.0 | Extra padding beyond `--margin`. |
| `--algorithm` | choice | `smart` | `smart` (layout-adaptive) or `legacy` (fixed parameters). |
| `--preview` | flag | off | Save preview PNGs alongside PDFs. |
| `--preview-only` | flag | off | Save previews only; no cropped PDFs. |
| `--naming` | choice | `per-page` | `per-page` or `flat` filename strategy. |
| `--output-prefix` | str | `figure` | Output filename stem. |
| `--log-level` | str | `INFO` | Logging verbosity. |
| `--save-config` | flag | off | Dump effective config to `output/config.json`. |
| `--config` | path | — | Load JSON config that overrides defaults. |
| `--no-color` | flag | off | Disable ANSI colour output. |

### Tuning recipes

Under the default `smart` algorithm the split is decided from the page's own
measurements, so most of these do not apply — raise `--dpi` first, then fall
back to the GUI's manual boxes for the pages that are still wrong.

| Symptom | Try |
| ------- | --- |
| One figure split into many fragments | Raise `--dpi` (300 – 600). On `legacy`, also increase `--morph-kernel-multiplier` (1.5 – 2.0). |
| Two close figures merged | Lower `--min-area` so a thin panel is not discarded. On `legacy`, decrease `--merge-gap-mm` (0.5 – 1.0). |
| Bounding box has unwanted padding around sparse figures | Decrease `--margin`. On `legacy`, also decrease `--merge-gap-mm`. |
| A page the detector gets wrong no matter what | Use the GUI's manual boxes for that page; it is exported with your rectangles while the rest stay automatic. |
| Fragments too small to see in the preview | Decrease `--min-area` / `--min-width` / `--min-height`. |
| Tiny noise blobs detected as figures | Increase `--min-area`. |
| Page text falsely detected as a figure | Increase `--threshold` (e.g. 250) or `--min-area`. |

---

## Preview mode

`--preview` (and `--preview-only`) writes a PNG per page showing where the
detector thinks each figure lives.  Boxes are drawn in red with green labels
("FIG 1", "FIG 2", …).  Always run preview first when working with an
unfamiliar source — it saves a lot of trial-and-error.

```bash
# Quick check before committing to crops:
python -m pdf_auto_crop.cli --input layout.pdf --preview-only

# Like the above but keep the cropped PDFs too:
python -m pdf_auto_crop.cli --input layout.pdf --preview
```

The preview is raster; the **output PDF is still vector**.  The preview boxes are
the *detected* bounds, so they do not include `--margin` — the white border is
applied at export.  (The GUI draws both: a dashed line for the detected content
and a solid frame for the final crop window.)

---

## Programmatic use

Every CLI flag maps 1-to-1 to fields on `pdf_auto_crop.config.CropConfig`.
The library entry points are stable:

```python
import pymupdf as fitz
from pdf_auto_crop.config import CropConfig
from pdf_auto_crop.smart_detector import smart_detect_figures   # the default
from pdf_auto_crop.detector import detect_figures              # == "legacy"
from pdf_auto_crop.geometry import mm_to_pt, add_margin_pt
from pdf_auto_crop.pdf_processor import render_page_to_array, crop_and_save_pdf

doc = fitz.open("input.pdf")
cfg = CropConfig(margin_mm=5.0)
rgb, page_pt = render_page_to_array(doc, 0, cfg.dpi)
rects = smart_detect_figures(rgb, page_pt, cfg)
page_rect = fitz.Rect(0, 0, page_pt[0], page_pt[1])
for i, rect in enumerate(rects):
    cropped = add_margin_pt(rect, mm_to_pt(cfg.margin_mm), page_rect)
    crop_and_save_pdf("input.pdf", 0, cropped, f"figure_{i+1}.pdf")
doc.close()
```

`rect` and the result of `smart_detect_figures` are both in PDF user space
(bottom-left origin) — the same convention `crop_and_save_pdf` expects.  See
[Coordinate convention](#coordinate-convention).

---

## Known limitations

* **Thin-line figures with very sparse content** (e.g. a single outline plus
  one label) may be detected as multiple disconnected fragments.  Mitigation:
  raise `--dpi`, or draw the box by hand in the GUI.  (On `--algorithm legacy`,
  increase `--morph-kernel-multiplier` instead.)
* **Two figures sharing a border** with no whitespace between them will be
  merged (a real border cannot be distinguished from a single figure's
  outline).  Add whitespace in the source, split them by hand, or if this
  matters — use manual boxes.
* **Hollow rectangles drawn only as outlines** with no interior content may
  be filtered if they fall below `--min-area`.  Decrease `--min-area` or
  add some interior ink.
* **Pages with extensive background tint** (e.g. coloured watermark) need a
  custom `--threshold` lower than 245.
* **Multi-page documents** are processed sequentially; each page's figures
  are emitted as `figure_p<page>_f<fig>.pdf`.
* **Cropped files keep the whole page's resources.**  `set_cropbox` clips what
  a viewer *shows*; it does not delete the content streams outside the frame.
  Splitting one dense A4 page into five figures therefore yields five files
  that each still embed the page's full image set — expect roughly
  `page size × figure count` in total output.  The figures open and print
  correctly; they are simply not as small as they look.  Trimming the off-crop
  content would need a content-rewrite pass, which risks the vector fidelity
  that is the whole point of this tool, so it is deliberately not done.

### Coordinate convention

Every rectangle in this package is in **PDF user space**: origin at the
bottom-left of the page, y increasing upward.  That includes the detector's
output, `CropConfig`-driven margins, and `crop_and_save_pdf`'s `rect`
argument.

PyMuPDF is the exception — its origin is top-left — so
`Page.set_cropbox` is given a y-flipped copy.  That conversion is the single
place the two conventions meet, in `pdf_processor.crop_and_save_pdf`.  Getting
it wrong mirrors every crop about the page's horizontal centre, which looks
fine for a single centred figure and lands on entirely the wrong artwork for a
multi-figure page.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| A cropped figure shows the *wrong* artwork | Old output files from a run before the CropBox coordinate fix | Re-export. Existing files are not rewritten in place. |
| All figures detected as one | Figures genuinely touch, or `legacy`'s gap is too large | Add whitespace in the source, or draw the boxes by hand in the GUI. On `legacy`, lower `--merge-gap-mm` to 0.5. |
| Zero figures detected | Threshold too low; everything is "foreground" | Raise `--threshold` to 248 – 252. |
| Text outside figures detected as a figure | Title text has its own connected component | Increase `--min-area` to 100 mm². |
| Cropped PDF has wrong page dimensions | Margin pushed the box outside the page | Lower `--margin`. |
| GUI frame looks bigger than the figure | That is the `--margin` white border; the dashed line marks the content | Expected. Set `白边` to 0 for a tight crop. |
| GUI frame does not move when the margin changes | Nothing loaded yet | Load a PDF first, then edit. |
| `--merge-gap-mm` / `--morph-kernel-multiplier` seem to do nothing | Expected under `--algorithm smart` (the default) | Use `--algorithm legacy` to make them effective. |
| `pip install` warning about `opencv-python` ABI mismatch | Mixing `opencv-python` and `opencv-python-headless` | `pip uninstall opencv-python-headless && pip install opencv-python`. |
| Garbled Chinese characters in Windows terminal | PowerShell/cmd default encoding is GBK (cp936) | Set `$env:PYTHONIOENCODING = "utf-8"` before running, or use Windows Terminal + "Beta: Use Unicode UTF-8 for worldwide language support" enabled in Windows settings. |
| No coloured output / progress bar shows as `████░░` literal text | Terminal does not honour ANSI escapes | Use Windows Terminal or any modern terminal; the package re-uses Python's logging handler so logs themselves stay readable even without colour. |
| Progress bar fails with `UnicodeEncodeError: 'gbk' codec` | Windows console codec cannot represent `░` (`U+2591`) | Already auto-reconfigured on first call; if it persists, set `$env:PYTHONIOENCODING = "utf-8"`. |

---

## Verifying vector preservation

After a successful crop, the output PDF should still contain real vector
content.  Quick verification:

```python
import pymupdf as fitz

doc = fitz.open("output/figure_p01_f01.pdf")
page = doc[0]
print("drawings:", len(page.get_drawings()))   # expect > 0
print("text:    ", len(page.get_text()))        # expect > 0 if source had text
print("images:  ", len(page.get_images()))       # expect 0 -- no raster inserted
doc.close()
```

The test suite (`tests/test_vector_preservation.py`) automates this check
across all fixtures.

### Verifying the crop is in the *right place*

Vector preservation only proves the content survived — not that the CropBox
points at the figure you meant.  A crop aimed at the wrong region still looks
like a perfectly valid vector PDF, which makes this worth checking once after
upgrading, and after any change to the crop path:

```python
import pymupdf as fitz

for name in ("figure_p01_f01.pdf", "figure_p01_f02.pdf"):
    doc = fitz.open(f"output/{name}")
    print(name, tuple(round(v, 1) for v in doc[0].cropbox))
    doc.close()
```

The CropBox is reported in PyMuPDF's **top-down** convention, so convert before
comparing against detector output (which is bottom-up):

```python
cb, h = doc[0].cropbox, doc[0].mediabox.height
user_space = fitz.Rect(cb.x0, h - cb.y1, cb.x1, h - cb.y0)
```

`tests/test_gui_edit.py` covers this empirically — markers in four distinct
colours, cropped, then rasterised to confirm the right one came back — because
a pure coordinate comparison cannot detect a flip.

---

## Project layout

```
pdf-auto-crop/
├── pyproject.toml
├── requirements.txt
├── README.md
├── src/
│   └── pdf_auto_crop/
│       ├── __init__.py
│       ├── __main__.py         # `python -m pdf_auto_crop` -> GUI or wizard
│       ├── batch.py            # batch scanning + processing
│       ├── cli.py              # argparse CLI + wizard bootstrap
│       ├── config.py           # CropConfig dataclass
│       ├── detector.py         # detect_figures() — the "legacy" algorithm
│       ├── geometry.py         # mm ↔ pt, raster ↔ PDF rect math
│       ├── gui.py              # tkinter window (preview + manual editor)
│       ├── gui_edit.py         # manual box store + canvas ↔ PDF geometry
│       ├── gui_hints.py        # detection feedback engine
│       ├── history.py          # ~/.pdf_auto_crop.json memory
│       ├── image_processor.py  # binarize / close / components / merge
│       ├── interactive.py      # 4-step wizard
│       ├── logging_setup.py    # shared logger (UTF-8 on Windows)
│       ├── namer.py            # per-page / flat filename strategies
│       ├── pdf_processor.py    # render + CropBox-based save
│       ├── pipeline.py         # single-file run_pipeline (used by CLI + batch)
│       ├── presets.py          # tight / standard / loose presets
│       ├── preview.py          # PNG overlay generation
│       ├── smart_detector.py   # layout-adaptive detector (default)
│       └── ui_utils.py         # ANSI colours, prompts, progress bar
├── scripts/
│   ├── make_test_pdfs.py       # Generates the 7 fixtures
│   ├── compare_algorithms.py   # legacy vs smart across all fixtures
│   ├── gui_screenshot.py       # Headless GUI smoke test + screenshot
│   └── gui_edit_screenshot.py  # Drives the manual editor with real events
└── tests/
    ├── fixtures/               # generated by scripts/make_test_pdfs.py
    ├── fixtures_diag/          # generated D1-D8 layout-diagnostic PDFs
    ├── test_batch.py           # batch scanning + per-PDF processing
    ├── test_end_to_end.py      # CLI invocation tests, one per case
    ├── test_gui.py             # hint engine + GUI worker bodies
    ├── test_gui_edit.py        # manual editor geometry + store
    ├── test_interactive.py     # presets, ui_utils, history, wizard steps
    ├── test_preview.py         # --preview / --preview-only tests
    ├── test_smart_detector.py  # gap histogram / bimodality / kernel ladder
    └── test_vector_preservation.py
```

---

## License

MIT.  See source headers for details.