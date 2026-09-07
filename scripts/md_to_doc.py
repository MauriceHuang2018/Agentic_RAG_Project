# -*- coding: utf-8 -*-
"""Convert a markdown file into a styled Word document (.doc) using python-docx.

Styling follows the same conventions as scripts/gen_competitive_analysis.py:
- CJK font handling via explicit w:rFonts (ascii/eastAsia/hAnsi + eastAsia hint)
- Table header row shading (D9E1F2) + bold, thin gray borders
- Title 18pt bold centered, H2 14pt bold, H3 12pt bold, body 10.5pt

Usage:
    python scripts/md_to_doc.py <input.md> <output.doc> [--landscape] [--font-size 10.5]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

DEFAULT_FONT = "微软雅黑"
HEADER_FILL = "D9E1F2"
NOTE_FILL = "F2F2F2"

# Inline emphasis: **bold**, *italic*, `code`
INLINE_RE = re.compile(r"(\*\*.+?\*\*|\*[^*]+?\*|`[^`]+?`)")


def set_cn_font(run, font_name: str = DEFAULT_FONT, size=None, bold=None, italic=None, color=None):
    """Apply CJK font + size + bold/italic/color to a run."""
    run.font.name = font_name
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:ascii"), font_name)
    rFonts.set(qn("w:eastAsia"), font_name)
    rFonts.set(qn("w:hAnsi"), font_name)
    rFonts.set(qn("w:hint"), "eastAsia")
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if italic is not None:
        run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color


def add_runs_with_inline(paragraph, text: str, *, size: float, bold=False, italic=False, color=None):
    """Add runs to a paragraph, converting **bold** / *italic* / `code` markers."""
    for token in INLINE_RE.split(text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**") and len(token) > 4:
            run = paragraph.add_run(token[2:-2])
            set_cn_font(run, size=size, bold=True, italic=italic, color=color)
        elif token.startswith("`") and token.endswith("`") and len(token) > 2:
            run = paragraph.add_run(token[1:-1])
            set_cn_font(run, font_name="Consolas", size=size, bold=bold, italic=italic, color=color)
        elif token.startswith("*") and token.endswith("*") and len(token) > 2:
            run = paragraph.add_run(token[1:-1])
            set_cn_font(run, size=size, bold=bold, italic=True, color=color)
        else:
            run = paragraph.add_run(token)
            set_cn_font(run, size=size, bold=bold, italic=italic, color=color)


def set_cell_shading(cell, hex_color: str):
    """Set a cell background color (hex without #)."""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def set_paragraph_shading(paragraph, hex_color: str):
    """Set a paragraph background color (hex without #)."""
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    pPr.append(shd)


def set_table_borders(table):
    """Apply a thin single-line border to all sides + inside of a table."""
    tbl = table._tbl
    tblPr = tbl.find(qn("w:tblPr"))
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    tblBorders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        b = OxmlElement(f"w:{edge}")
        b.set(qn("w:val"), "single")
        b.set(qn("w:sz"), "4")
        b.set(qn("w:space"), "0")
        b.set(qn("w:color"), "808080")
        tblBorders.append(b)
    tblPr.append(tblBorders)


def fill_cell(cell, text: str, *, size: float, bold=False, align=None, fill=None):
    """Replace cell content with a single styled paragraph (inline emphasis aware)."""
    cell.text = ""
    p = cell.paragraphs[0]
    if align is not None:
        p.alignment = align
    add_runs_with_inline(p, text, size=size, bold=bold)
    if fill is not None:
        set_cell_shading(cell, fill)


def split_table_row(line: str) -> list[str]:
    """Split a markdown table row into cell texts (no leading/trailing pipes)."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    # Split on unescaped pipes (cells containing \| keep the literal pipe)
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", stripped)]


def is_separator_row(cells: list[str]) -> bool:
    """Detect a markdown table separator row like |---|:---:|."""
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)


def parse_markdown(md_text: str) -> list[dict]:
    """Parse markdown into a flat list of blocks: heading/para/quote/table/rule."""
    blocks: list[dict] = []
    lines = md_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("#"):
            m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
            if m:
                blocks.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2).strip()})
                i += 1
                continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            blocks.append({"type": "rule", "text": ""})
            i += 1
            continue
        if stripped.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append({"type": "quote", "text": " / ".join(q for q in quote_lines if q)})
            continue
        if stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_table_row(lines[i]))
                i += 1
            # Drop separator rows
            rows = [r for r in rows if not is_separator_row(r)]
            if rows:
                blocks.append({"type": "table", "rows": rows})
            continue
        # Paragraph: accumulate consecutive plain lines
        para_lines = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith("#")
                or nxt.startswith("|")
                or nxt.startswith(">")
                or re.fullmatch(r"-{3,}|\*{3,}|_{3,}", nxt)
            ):
                break
            para_lines.append(nxt)
            i += 1
        blocks.append({"type": "para", "text": "".join(para_lines)})
    return blocks


def add_heading(doc, text: str, level: int, *, body_size: float):
    """Add a heading paragraph sized by markdown level."""
    sizes = {1: 18, 2: 14, 3: 12, 4: 11, 5: 10.5, 6: 10.5}
    size = sizes.get(level, body_size)
    p = doc.add_paragraph()
    if level == 1:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(10 if level > 1 else 0)
    p.paragraph_format.space_after = Pt(6)
    add_runs_with_inline(p, text, size=size, bold=True)
    return p


def add_quote(doc, text: str, *, body_size: float, is_first_block: bool):
    """Render a markdown blockquote: doc meta line (centered) or a shaded note."""
    p = doc.add_paragraph()
    if is_first_block:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        add_runs_with_inline(p, text, size=9, italic=False)
    else:
        p.paragraph_format.left_indent = Cm(0.5)
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after = Pt(4)
        set_paragraph_shading(p, NOTE_FILL)
        add_runs_with_inline(p, "注：" + text if not text.startswith("注") else text, size=body_size - 1)
    return p


def add_table(doc, rows: list[list[str]], *, body_size: float, landscape: bool):
    """Render a markdown table with shaded bold header row and gray borders."""
    n_cols = max(len(r) for r in rows)
    n_rows = len(rows)
    table = doc.add_table(rows=n_rows, cols=n_cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    set_table_borders(table)
    # Column widths: narrow first column (label-like), remaining split evenly
    total_cm = 26.7 if landscape else 16.0
    first_w = min(2.6, total_cm / n_cols)
    rest_w = (total_cm - first_w) / (n_cols - 1) if n_cols > 1 else total_cm
    for idx, col in enumerate(table.columns):
        col.width = Cm(first_w if idx == 0 else rest_w)
    for r, row in enumerate(rows):
        for c in range(n_cols):
            text = row[c] if c < len(row) else ""
            if r == 0:
                set_cell_shading(table.cell(r, c), HEADER_FILL)
                fill_cell(table.cell(r, c), text, size=body_size, bold=True,
                          align=WD_ALIGN_PARAGRAPH.CENTER)
            else:
                fill_cell(table.cell(r, c), text, size=body_size,
                          bold=(c == 0 and n_cols >= 4))
    # Spacer paragraph after table (Word tables glue to the next paragraph otherwise)
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(0)
    run = spacer.add_run("")
    set_cn_font(run, size=4)
    return table


def convert(md_path: Path, doc_path: Path, *, landscape: bool, body_size: float) -> None:
    """Convert one markdown file to one Word document."""
    md_text = md_path.read_text(encoding="utf-8")
    blocks = parse_markdown(md_text)
    doc = Document()
    # Page setup: A4, landscape optional, moderate margins
    section = doc.sections[0]
    if landscape:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = Cm(29.7), Cm(21.0)
        section.left_margin = section.right_margin = Cm(1.5)
        section.top_margin = section.bottom_margin = Cm(1.5)
    else:
        section.page_width, section.page_height = Cm(21.0), Cm(29.7)
        section.left_margin = section.right_margin = Cm(2.5)
        section.top_margin = section.bottom_margin = Cm(2.5)
    doc.styles["Normal"].font.name = DEFAULT_FONT
    doc.styles["Normal"].font.size = Pt(body_size)
    doc.styles["Normal"]._element.rPr.rFonts.set(qn("w:eastAsia"), DEFAULT_FONT)

    is_first_block = True
    for block in blocks:
        btype = block["type"]
        if btype == "heading":
            add_heading(doc, block["text"], block["level"], body_size=body_size)
        elif btype == "para":
            p = doc.add_paragraph()
            add_runs_with_inline(p, block["text"], size=body_size)
        elif btype == "quote":
            add_quote(doc, block["text"], body_size=body_size, is_first_block=is_first_block)
        elif btype == "table":
            add_table(doc, block["rows"], body_size=body_size, landscape=landscape)
        elif btype == "rule":
            continue  # horizontal rules carry no Word content
        is_first_block = False
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(doc_path)
    print(f"OK: {doc_path} ({len(blocks)} blocks)")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Convert markdown to a styled Word document.")
    parser.add_argument("input", help="input markdown file path")
    parser.add_argument("output", help="output Word file path (.doc/.docx)")
    parser.add_argument("--landscape", action="store_true", help="use A4 landscape (wide tables)")
    parser.add_argument("--font-size", type=float, default=10.5, help="body font size in pt")
    args = parser.parse_args(argv)
    md_path = Path(args.input)
    if not md_path.is_file():
        print(f"ERROR: input not found: {md_path}", file=sys.stderr)
        return 2
    convert(md_path, Path(args.output), landscape=args.landscape, body_size=args.font_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
