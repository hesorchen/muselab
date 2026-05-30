---
name: pptx
description: USE WHEN a user wants to create a PowerPoint (.pptx) presentation or slides — generates a deck programmatically with python-pptx, adding a title slide, content slides with headings and bullet points, and saving the result as a .pptx file.
---

# PPTX creation

## Overview

Create PowerPoint (.pptx) presentations programmatically using the cross-platform
[`python-pptx`](https://python-pptx.readthedocs.io/) library. This works on macOS,
Linux, and Windows with no native Office install and no bundled scripts — you write
and run a short Python program with the Bash tool.

## Dependencies

```bash
pip install python-pptx
```

That is the only dependency. Do not rely on LibreOffice, `apt-get`, or any
platform-specific tooling.

## Workflow

1. **Plan the deck**: Decide the title and the list of content slides (each with a
   heading and a few bullet points). Keep bullets short — aim for 3–6 per slide.
2. **Design choices**: Briefly state the intended tone/palette before writing code if
   the user cares about styling; otherwise the default template is fine.
3. **Generate the file**: Run a Python program (below) that builds and saves the deck.
4. **Report the output path** so the user can open the .pptx.

## Generating a presentation

Run inline Python with the Bash tool. The example below creates a title slide plus
content slides with bullet points:

```bash
python3 - <<'PY'
from pptx import Presentation
from pptx.util import Pt

prs = Presentation()  # default 4:3; use Presentation() then set slide size for 16:9 if needed

# --- Title slide (layout 0: Title + Subtitle) ---
slide = prs.slides.add_slide(prs.slide_layouts[0])
slide.shapes.title.text = "Quarterly Review"
slide.placeholders[1].text = "Prepared for the team"

# --- Content slides (layout 1: Title + Content) ---
sections = [
    ("Highlights", ["Revenue up 12%", "Two new customers", "Shipped v2.0"]),
    ("Risks",      ["Hiring behind plan", "Vendor delay on hardware"]),
    ("Next Steps", ["Finalize Q3 roadmap", "Close the open req", "Renew contract"]),
]
for heading, bullets in sections:
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = heading
    body = slide.placeholders[1].text_frame
    body.clear()
    for i, point in enumerate(bullets):
        para = body.paragraphs[0] if i == 0 else body.add_paragraph()
        para.text = point
        para.level = 0
        para.font.size = Pt(18)

prs.save("presentation.pptx")
print("Saved presentation.pptx")
PY
```

### Useful building blocks

- **16:9 slides**: after `prs = Presentation()`, set
  `prs.slide_width = Pt(960); prs.slide_height = Pt(540)`.
- **Common layouts** in the default template:
  `slide_layouts[0]` = Title slide, `[1]` = Title + Content, `[5]` = Title only,
  `[6]` = Blank.
- **Speaker notes**:
  `slide.notes_slide.notes_text_frame.text = "Talking points..."`
- **Plain text box** (on a blank layout):
  `tb = slide.shapes.add_textbox(left, top, width, height); tb.text_frame.text = "..."`
  with positions in `Inches(...)` or `Pt(...)`.
- **Indented sub-bullets**: set `para.level = 1` (or higher) on a body paragraph.
- **Bold / size**: `run = para.runs[0]; run.font.bold = True; run.font.size = Pt(24)`.

## Reading an existing presentation

To read the text of an existing deck, iterate its shapes with python-pptx:

```bash
python3 - <<'PY'
from pptx import Presentation
prs = Presentation("input.pptx")
for n, slide in enumerate(prs.slides):
    print(f"--- Slide {n} ---")
    for shape in slide.shapes:
        if shape.has_text_frame:
            print(shape.text_frame.text)
PY
```

## Notes and limitations

- `python-pptx` covers text, bullets, tables, images, and basic shapes/charts. It does
  not render slides to images and does not do pixel-perfect HTML layout — keep designs
  bullet- and text-driven.
- For richer visuals (charts, images), add them with python-pptx's
  `add_chart` / `add_picture` APIs rather than external converters.
- Always confirm the saved file path in your reply.
