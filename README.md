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

The same idea carries the other way round: the **组合** (compose) window takes
figures from several PDFs and writes them onto one sheet as vector content
with real, selectable labels — no rasterising anywhere.

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

The suite is 386 tests.  Three of them deserve a note, because they encode bugs
that a coordinate comparison cannot see:

- `test_crop_lands_on_the_requested_region` puts a marker of a unique colour in
  each of the four page corners, crops around one, and rasterises the result to
  confirm the right colour came back.  A vertical flip — the failure mode of a
  PyMuPDF/PDF coordinate-convention mix-up — is invisible to any test that
  compares numbers in a single convention.
- `test_shown_boxes_equal_the_exported_cropbox` asserts that what the GUI draws
  is what lands in the file, including the white border.
- `test_bake_reproduces_what_the_viewer_sees` does the same for the
  compositor: it renders the source page, finds the coloured figure in the
  *pixels*, and demands the baked figure match it — for every page rotation.
  The oracle involves no coordinate arithmetic at all, so a mistake in the
  bake cannot satisfy both sides.  (It has to be a non-square page with an
  off-centre figure: a square page with a centred figure is symmetric under
  every rotation and hides exactly the mistakes the test is for.)

The parts a unit test cannot reach — the canvas, the tools, the property
panel — are exercised end to end by the scripts, which drive the **real**
windows with synthetic mouse events and check what comes out:

```bash
python scripts/gui_edit_screenshot.py     # the cropping window
python scripts/compositor_screenshot.py   # the compose window
python scripts/gui_compositor_button.py   # the 组合… button on the main window
python scripts/gui_screenshot.py          # the main window
```

`compositor_screenshot.py` is the one that covers the compositor: it drops
figures, drags and resizes them, draws every kind of graphic element, types
free text and a LaTeX formula, turns things, aligns and spreads a group,
zooms with the wheel, and finally checks the exported PDF — the vector
content, the fonts, the dash patterns and the opacities.

Both `diag_out/` and the generated fixtures are git-ignored.

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

## Composing figures

The cropping window splits one page into its figures.  The **组合** (compose)
window does the other half of the job: take figures from *several* PDFs,
arrange them on one sheet, number them, and write the result as a single PDF.

Open it with the **组合…** button in the main window's toolbar.

### The workflow

1. **导入 PDF…** — pick one or more PDFs (the dialog takes a whole selection)
   and the same detector used for cropping finds the figures on every page;
   they land in the palette on the left.  **导入图片…** brings in PNG, JPEG
   and the rest: a picture is turned into a page of its own first, so it can
   be placed and aligned like any other figure.  **导入整页…** brings the
   pages in as they are, nothing cut off — that is the way to take in a
   figure the detector would split or trim.  **导入已裁剪的图…** takes one
   or more PDFs that are already one figure per page and uses each page
   whole.
   A file that cannot be read, or that holds no figure, is reported and the
   rest still come in.

   The detector measures the **ink**, so a very pale element — or one that
   sits just outside the ink — can end up cut off.  **识别留白** (in the
   palette) keeps a margin around every detected figure; raise it when
   something is missing, or use 导入整页… when in doubt.
2. **Double-click** a palette entry to drop it onto the sheet, or drag it in
   from the list.  **删除选中** takes an entry back out of the palette —
   and the figure it had on the sheet goes with it, because it is the same
   picture.  **清空素材** empties the palette altogether.
   The name of a file says nothing about the picture in it, so **resting the
   cursor on an entry shows the figure**, floating over the palette.
3. **Drag** a figure to move it, drag an orange corner handle to resize it.
4. **Type a label** in the properties panel — `(a)`, `(b)`, … — or let
   **自动编号** number them in order.  The label's font, size and colour are
   editable too (see [Labels](#labels-font-size-and-colour)).
5. **Pick a tool** in the toolbar to draw arrows, boxes, circles or lines on
   the sheet, or to drop a piece of text (see [Graphic elements](#graphic-elements)
   and [The view](#the-view)).
6. **导出 PDF…** writes the sheet.

### On the sheet

| Gesture | Effect |
| ------- | ------ |
| Drag a figure | Move it |
| Drag an orange corner handle | Resize, keeping the figure's aspect ratio |
| `Ctrl`+click | Add to (or take out of) the selection |
| Arrow keys | Nudge 1 pt; hold `Shift` for 10 pt |
| `Delete` | Remove everything that is selected |
| 置于顶层 / 置于底层 | Change the stacking order |

A scientific figure must not be stretched, so resizing keeps the aspect
ratio: the figure grows away from the corner opposite the handle you drag.

**横向排列** lays the figures out in a row, **网格排列** in a grid.  Both give
every figure the same cell, so the sheet comes out even rather than ragged;
a figure whose aspect differs from the cell is fitted into it, not
stretched.

Labels are **图下** (centred under the figure — the journal convention) by
default; **角上** puts them in the corner of the figure instead.

### Aligning

Select several objects with `Ctrl`+click (or `Shift`+click) and the **对齐**
buttons line them up:

| Button | Effect |
| ------ | ------ |
| 左 / 中 / 右 | The left edges, the centres or the right edges meet |
| 顶 / 中 / 底 | The top edges, the centres or the bottom edges meet |

The selection may **mix kinds**: a figure, a box and a note can be lined up
against each other in one go.  (A plain click still starts afresh; `Ctrl`+click
adds to what is already picked.)

With more than one object selected they are lined up against *each other* —
"align left" puts every left edge on the leftmost one.  With a single object
there is nobody to line up with, so it is pushed against the edge of the
sheet instead.

Aligning moves objects; it never resizes them, so a figure keeps the size
and the aspect ratio it was given.

**分布** spreads a group out instead: the two outermost objects stay where
they are and the ones in between are moved so that the gaps between them come
out equal — 水平等间距 and 垂直等间距 for the two directions.

**一样大** makes the first selected object the size of the second: 同宽
matches the width, 同高 the height (both keeping the aspect ratio), and 等大
makes it exactly the other's size, which may stretch it.  Pick the object to
change first and the one to copy second.  It works on every kind of object:
figures, graphic elements, free text and labels — a text changes its size by
changing the size of its letters.

**矩阵阵列…** lays the selected figures out in a grid of as many rows and
columns as you ask for.

A figure's number travels with the figure, so lining the figures up lines
the numbers up too.  A number can also be **picked on its own** — click it
and it is the label that is selected, so several numbers can be lined up
without moving the figures they belong to.

### Groups

**组合** (or `Ctrl`+`G`) makes the selection one object that travels
together; **取消组合** (or `Ctrl`+`U`) lets it go its own ways again.  Clicking
any member of a group picks the whole group, and `Ctrl`+click adds it to the
selection.  A group is remembered in the saved layout, and the objects keep
their own place in the sheet — grouping changes how they are *handled*, not
what the exported PDF contains.

### Labels: font, size and colour

Each label carries its own font, size and colour, so a figure can use the
journal's typeface while the rest stay in the default.

| Control | What it does |
| ------- | ------------ |
| 字体 | One of twelve built-in faces — Times, Helvetica, Courier, Symbol, ZapfDingbats and their bold/italic variants, plus 宋体 for Chinese |
| 文件… | Use a `.ttf`/`.otf` from disk instead; the font is **embedded** in the exported PDF |
| 字号 | 4 – 48 pt |
| 颜色 | Any colour; the swatch next to it shows the current one |
| 加粗 / 斜体 | Bold and italic, on top of the face itself |
| LaTeX 公式 | Write the label as mathematics (see [Free text and formulas](#free-text-and-formulas)) |

The built-in faces are the ones Windows has — Times New Roman, Arial,
Courier New, Symbol, SimSun — and the file that holds them is **embedded** in
the output, as a subset of the glyphs used.  That is what makes the exported
page show the letters you arranged: a built-in "Times-Roman" is drawn by the
reader with whatever Times it has, which is a different face from the one on
the canvas and a different one from the body of a thesis set in Times New
Roman.  The subset costs a few kilobytes.

A font file you pick is embedded as well, in the same way.

If the chosen face has no glyph for a character — an ASCII face asked to
write 中文, say — the label falls back to a Chinese face automatically.  A
missing glyph is worse than a different typeface, so the fallback is silent
rather than a row of empty boxes.

The question is put to the *encoder*, not to the font's character map: a font
can hold a character and still be unable to write it — the full-width
brackets are in Times New Roman's map, but the encoder puts .notdef in the
file for them, and a label written that way is invisible.  A file you pick
yourself is asked the same question.

And the export checks itself: the written file is read back, and if a label
did not survive it, the sheet is written again with the built-in faces,
which every reader has and which always write the text.

On a machine that has none of these font files the export falls back to the
PDF base-14 faces, which every reader has.

**位置** puts the label where you want it:

| Position | Where it goes |
| -------- | ------------- |
| 图下 | Centred under the figure — the journal convention |
| 左上 | In the top-left corner of the figure |
| 左下 | In the bottom-left corner of the figure |
| 右下 | In the bottom-right corner of the figure |
| 自由 | Wherever you want it |

In 自由 you simply **drag the label** on the sheet: grab it and pull it
anywhere.  The offset is kept relative to the figure, so the label travels
with the figure when you move it.

### Free text and formulas

The 文本 and 文本框 tools drop a piece of text on the sheet: a note of your
own, a caption, a formula.  The text is written as real, selectable text, in
any of the faces above, with 字号, 颜色, 加粗, 斜体, an optional frame around
it and an 角度 to turn it by.  A turned text is written on a page of its own
and then placed with an angle, so it stays real text rather than a picture —
and it does not jump away when you turn it.

Ticking **LaTeX 公式** typesets the text as mathematics instead: `\frac{1}{2}`,
`\sum_{i=1}^{n} x_i`, `\alpha`, `\sqrt{2}` and the rest of the usual
notation.  Matplotlib's mathtext does the typesetting — no TeX installation
needed — and the result is drawn as **vector** PDF with the font embedded, so
it stays sharp and the symbols remain selectable.

Matplotlib is an optional dependency: without it a formula is written as
ordinary text — the LaTeX source as it stands — rather than failing.

### Graphic elements

The **工具** dropdown offers the shapes PowerPoint offers — 箭头 (a line with a
head), 方框, 圆角框, 圆圈, 三角形, 菱形, 平行四边形, 右箭头, 左箭头, 上箭头,
下箭头 and 直线 — plus 文本 and 文本框.  Pick one and drag on the sheet to
draw it, or **click** on the sheet to place one with a sensible size.

Once an element is selected, the properties panel styles it:

| Control | What it does |
| ------- | ------------ |
| 边框色 | The border colour |
| 边框宽 | The border width, 0.2 – 12 pt |
| 边框线型 | 实线 (solid), 虚线 (dashed) or 点线 (dotted) |
| 边框透明 | How opaque the border is, 0 – 100 % |
| 填充色 | The colour inside the shape |
| 填充 | Fill the inside at all |
| 填充透明 | How opaque the fill is, 0 – 100 % |
| 角度 | How far the element is turned, in degrees |
| 内容 | A caption in the middle of the shape, as in PowerPoint |

The styles apply to every selected element at once.  Elements can be dragged,
nudged with the arrow keys, copied (复制), flipped (水平/垂直翻转) and deleted
with `Delete`.

Only the *stroke* of an element is grabbable, so a translucent box drawn over
a figure does not stop you from selecting the figure underneath it.  When the
cursor is on both, the element wins — it is the thing drawn on top.

A figure — an embedded PDF page — cannot be mirrored: doing so would mean
rasterising it, and giving up the vector output the tool is about.  Turning
one is fine, though: a quarter turn swaps its width and height and keeps its
size.

Everything stays vector: an arrow is a line plus two short strokes for the
head, a box is a rectangle, a triangle is a polygon.  The dash pattern and
the opacities are written into the PDF as such.  No bitmap is introduced
anywhere in the composition.

### The view

| Gesture | Effect |
| ------- | ------ |
| Mouse wheel | Zoom in or out, around the cursor |
| Middle-button drag | Pan the view |
| − / + in the footer | Zoom in or out |
| 适应窗口 in the footer | Back to the view that fits the sheet |

A click is not a drag: the cursor has to travel a few pixels before anything
moves, so picking a figure to select it never nudges it out of a layout you
have just arranged.

**Dragging snaps.**  The edges of the object you drag are pulled onto the
edges of the objects around it — left to left, middle to middle, and so on —
when they come within four points.  A row of figures lines itself up.

### Undo and redo

**Ctrl+Z** takes back the last change, **Ctrl+Y** (or Ctrl+Shift+Z) does it
again.  Fifty steps are kept.  A drag is one step, and so is a button in
the toolbar or the properties panel.

### The page

**纸张** picks the page: A4 横向, A4 纵向, 适应内容 (as large as the content)
or **自定义**, where the width and height are given in millimetres.  The
arrangements (横向/网格) shrink their cells to fit the page, and keep room
under the bottom row for the labels.

**适应内容** takes the size of the content *and brings the content with it*:
the figures move to the margin of the new page, because a page that is
resized around content that stays where it was would leave half of it
outside the sheet.

The sheet is an object too: its edges and corners carry grips, and dragging
one makes the page bigger or smaller.  **The edge you grab follows the
mouse** and the opposite one stays where it was — pulling the top edge up
makes the page taller, and the content stays where it was.

### What the output is

The sheet is written with the same vector-preserving mechanism as the
cropper: each figure is embedded as a Form XObject (the same thing LaTeX's
`\includegraphics` does for PDF input) and each label is inserted as real,
selectable text.  The graphic elements are drawn as real vector paths.  Nothing
is rasterised, so the result stays sharp at any zoom and the text can be
searched and copied.

**It goes straight into a LaTeX document.**  The exported PDF is a normal PDF:
`\includegraphics` takes it, and both `pdflatex` and `xelatex` compile it
without complaint — figures, dashed and translucent boxes, formulas, turned
panels and coloured labels all survive.  A formula is drawn with an embedded
TrueType font, so it needs no LaTeX package on your side.

The exported page is either the sheet you arranged (A4 landscape or
portrait) or, with **适应内容**, exactly as large as the content — which is
what you want for a figure going into a manuscript.

### Giving an old export the new face

The labels of an export made **before** the font fix carry the PDF base-14
faces: the reader draws those with whatever Times it has, which is not the
Times New Roman the canvas showed.  `scripts/migrate_label_font.py` writes
those labels again in the face the canvas draws with — the very file,
embedded — and leaves the original alone:

```bash
python scripts/migrate_label_font.py path/to/figures
```

The labels are the page's own text; the figures are Form XObjects, and their
text lives inside them.  Each label of the old build sits in a content
stream of its own, so the safest way to take it away is to empty that stream:
the artwork is not touched, and a cropped page keeps its crop.

### Editing the text inside a figure

An imported figure is an embedded PDF page, so its text comes with it: the
axis titles, the tick labels, the legend — all of it is still real text in the
output.

**编辑图中的文字…** (in the 标签 tab of the selected figure) recognises those
lines and lists them.  Each one can be rewritten, or emptied to take it away.
The old glyphs are removed and the new ones written in their place, so the
figure stays vector and the artwork around the text is untouched.

The rewritten line keeps the look of the original as far as possible: a line
that was in Times is written in Times, one in Helvetica in Helvetica.

**取图范围…** chooses the part of the source that becomes the figure: the
page is shown, the region that is currently taken is marked out, and
dragging a new box over the page replaces it.  The figure gets a copy of the
source with the new region, so the palette keeps the original and other
placements of it are not changed behind your back.

### The two windows

The cropping window and the compose window used to be two windows.  They are
**one window now**, with a **裁剪 / 组合** switch at the top: the two halves
of the job share the same place, and switching between them keeps
everything where you left it.

The division of labour is unchanged: the cropping half splits a page into
its figures; the compose half puts figures from anywhere onto one sheet.

After an export the cropping half offers to hand the figures it just wrote
over to the compose half, where they land in the palette ready to be
arranged.  You can also bring them across by hand — **导入已裁剪的图…**
takes a PDF that is already one figure per page.

### Known limits

- The output is large: MuPDF keeps the embedded source page's resources, so
  a figure lifted out of a dense, image-heavy paper carries the images with
  it.  Cropping the source page first (this tool's job) keeps the figure
  small.  The label fonts add a few kilobytes: they are embedded as subsets
  of the glyphs the sheet actually uses.
- The sheet holds one copy of each figure.  The same figure cannot be placed
  twice — import it twice if you need it twice.
- A **figure** cannot be mirrored: it is an embedded PDF page, and turning
  one inside out would mean rasterising it.  Turning it — rotating it — is
  fine.
- A line of text that was rewritten inside a figure is written in the closest
  built-in face, not in the original one: the original font file does not
  necessarily travel with the PDF.
- Without matplotlib installed, a formula is written as ordinary text — the
  LaTeX source as it stands — rather than failing.

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
11. **Fold in the figure's own labels.**  The detector finds the *ink* of a
    plot, so the axis ticks, the axis titles and the figure's title — a
    millimetre or two outside the frame — are not part of the box it
    returns, and a crop taken from that box loses them.  The page's own text
    says where they are, so the text blocks that belong to a detected
    figure are folded into it.  Captions ("Fig. 3: ...") and body text are
    left out: a caption belongs to the figure but is not part of it.

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

The compositor has the same kind of entry points:

```python
from pathlib import Path
import pymupdf as fitz
from pdf_auto_crop import compositor as comp

sheet = comp.Composition(width=842, height=595)        # A4 landscape, in points
for i, fig in enumerate(comp.detect_figures(Path("input.pdf")), start=1):
    w, h = comp.figure_display_size(fig)
    sheet.placements.append(comp.Placement(
        figure=fig,
        rect=fitz.Rect(20 + (i - 1) * 200, 250, 20 + (i - 1) * 200 + w, 250 + h),
        label=comp.auto_label(i),                       # "(a)", "(b)", ...
        label_font="Helvetica-Bold",                    # any of comp.LABEL_FONTS
        label_size=12.0,
        label_color=(0.8, 0.0, 0.0),                   # r, g, b in 0..1
    ))

# A graphic element on top of the figures: an arrow, a box or a circle.
sheet.shapes.append(comp.Shape(
    kind=comp.SHAPE_ARROW,
    start=fitz.Point(120, 240), end=fitz.Point(120, 320),
    color=(0.85, 0.1, 0.1), width=1.5,
    fill=True,                       # only a box or a circle can be filled
    fill_color=(1.0, 0.88, 0.54),
    fill_opacity=0.4,                # 0..1
    stroke_opacity=1.0,              # 0..1
    dash=comp.DASH_DASHED,           # "solid", "dashed" or "dotted"
))

comp.export_composition(sheet, Path("composed.pdf"), fit_to_content=False)
```

`detect_figures` returns the same rectangles the cropping pipeline produces,
and `export_composition` writes the sheet with the figures embedded as vector
and the labels as real text.  With `fit_to_content=True` (the default) the
page is sized to the content instead of the sheet.

A `Shape`'s two points are in the composition's user space, like everything
else here: an arrow runs from `start` to `end` and puts its head at `end`, and
a box or an ellipse is the rectangle spanned by the two points.  The drawing
order is figures, then graphic elements, then labels — a label under an arrow
is unreadable.

### Aligning objects

```python
from pdf_auto_crop import compositor as comp

# Put the panels on the same top edge.
rects = [p.rect for p in sheet.placements]
target = comp.align_target(rects, comp.ALIGN_TOP)      # the y they share
for p in sheet.placements:
    p.rect = comp.aligned_rect(p.rect, comp.ALIGN_TOP, target)
```

`align_target` works out the coordinate a group shares — the group's own
bounding box when there is more than one object, or the sheet when there is
only one — and `aligned_rect` moves a rectangle onto it.  Aligning moves
objects; it never resizes them.

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
│       ├── compositor.py       # figure compositor: model, bake, export
│       ├── compositor_gui.py    # compose window (palette + sheet + labels)
│       ├── config.py           # CropConfig dataclass
│       ├── detector.py         # detect_figures() — the "legacy" algorithm
│       ├── dpi.py              # high-DPI awareness for the windows
│       ├── figure_text.py      # folds a figure's axis labels into its box
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
│       ├── tex.py              # LaTeX mathematics → vector PDF
│       └── ui_utils.py         # ANSI colours, prompts, progress bar
├── scripts/
│   ├── make_test_pdfs.py       # Generates the 7 fixtures
│   ├── compare_algorithms.py   # legacy vs smart across all fixtures
│   ├── run_all_cases.py        # Runs every fixture through every algorithm
│   ├── diagnose.py             # Detects on one PDF and writes an overlay
│   ├── deep_diagnose.py        # The same, with the page's ink analysed
│   ├── diag_overlay_vs_export.py  # The overlay against the exported crop
│   ├── explore_gaps.py         # The gap histogram of a page
│   ├── make_previews.py        # Preview images of the fixtures
│   ├── compositor_screenshot.py # Drives the compose window with real events
│   ├── gui_compositor_button.py # Checks the 组合… button opens the window
│   ├── gui_screenshot.py       # Headless GUI smoke test + screenshot
│   ├── gui_edit_screenshot.py  # Drives the manual editor with real events
│   ├── probe_bake_fix2.py      # Measures the compositor's bake geometry
│   ├── probe_clip_rule3.py     # Measures the clip frame on odd page boxes
│   └── probe_normalised_bake.py # Verifies the two-step bake end to end
└── tests/
    ├── fixtures/               # generated by scripts/make_test_pdfs.py
    ├── fixtures_diag/          # generated D1-D8 layout-diagnostic PDFs
    ├── test_batch.py           # batch scanning + per-PDF processing
    ├── test_compositor.py      # compositor model, bake, export fidelity
    ├── test_dpi.py             # high-DPI awareness and window scaling
    ├── test_end_to_end.py      # CLI invocation tests, one per case
    ├── test_figure_text.py     # folding a figure's axis labels into its box
    ├── test_gui.py             # hint engine + GUI worker bodies
    ├── test_gui_edit.py        # manual editor geometry + store
    ├── test_interactive.py     # presets, ui_utils, history, wizard steps
    ├── test_preview.py         # --preview / --preview-only tests
    ├── test_smart_detector.py  # gap histogram / bimodality / kernel ladder
    ├── test_tex.py             # LaTeX mathematics rendered as vector PDF
    └── test_vector_preservation.py
```

---

## License

MIT.  See source headers for details.