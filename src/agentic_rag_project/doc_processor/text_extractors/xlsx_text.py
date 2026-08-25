"""XLSX extraction via openpyxl — one section per sheet.

DESIGN: parser_router/DESIGN_parser_router.md
- Each sheet becomes one `ParsedSection` (Q1 decision: XLSX NEVER goes through
  DeepDoc — openpyxl reads structure more accurately than any vision model).
- The first non-empty row is treated as headers; merged cells are unmerged
  in display so a single cell value fills the merged range (preserves the
  semantics a human reader sees).
- Each data row becomes one `ContentBlock(kind="table", text=...)` with a
  pipe-separated key/value representation that the chunker can split into
  parent + child chunks by row.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook  # type: ignore[import-not-found]
from openpyxl.cell.cell import MergedCell  # type: ignore[import-not-found]

from agentic_rag_project.doc_processor.models import (
    ContentBlock,
    ParsedDoc,
    ParsedSection,
)


def _cell_value(ws, row_idx: int, col_idx: int):
    """Read a cell, resolving merged-cell coordinates to their anchor value.

    Cells outside the used range raise `AttributeError` on `.row`/`.column`
    under openpyxl's `read_only=True` mode; we treat those as `None` so
    callers see a uniform value contract.
    """
    # Resolve merge membership purely from coordinates — openpyxl's
    # `MergedCell` isinstance check is unreliable in `read_only=True` mode
    # because the cell object can be a plain stub without the right type.
    for merged_range in ws.merged_cells.ranges:
        if (
            merged_range.min_row <= row_idx <= merged_range.max_row
            and merged_range.min_col <= col_idx <= merged_range.max_col
        ):
            anchor = ws.cell(row=merged_range.min_row, column=merged_range.min_col)
            try:
                return anchor.value
            except AttributeError:
                return None
    cell = ws.cell(row=row_idx, column=col_idx)
    try:
        cell.value
    except AttributeError:
        return None
    return cell.value


def _format_row(headers: list[str], values: list, row_idx: int) -> str:
    """Render one data row as a pipe-separated key/value string."""
    parts: list[str] = []
    for header, value in zip(headers, values):
        parts.append(f"{header}: {value}")
    return " | ".join(parts)


def extract_xlsx(file_path: str | Path) -> ParsedDoc:
    """Extract an XLSX into a `ParsedDoc` with one section per sheet.

    Raises:
        FileNotFoundError: when `file_path` does not exist.
        openpyxl.utils.exceptions.InvalidFileException: when the file is not
            a valid XLSX zip.
    """
    path = Path(file_path)
    # We need `merged_cells.ranges` so we can't use `read_only=True` here —
    # ReadOnlyWorksheet exposes no merge info.
    workbook = load_workbook(filename=str(path), data_only=True, read_only=False)
    try:
        sections: list[ParsedSection] = []
        for sheet_idx, sheet_name in enumerate(workbook.sheetnames):
            ws = workbook[sheet_name]
            rows_iter = ws.iter_rows(values_only=False)
            try:
                first_row = next(rows_iter)
            except StopIteration:
                sections.append(
                    ParsedSection(
                        heading=sheet_name or f"Sheet {sheet_idx + 1}",
                        level=1,
                        blocks=[],
                        page_start=sheet_idx,
                        page_end=sheet_idx,
                    )
                )
                continue
            # `iter_rows()` can yield `EmptyCell` placeholders outside the
            # used range; fetch values by `(row, col)` instead of relying on
            # `cell.row` / `cell.column` attributes on the cell object.
            header_row_idx = 1
            headers: list[str] = []
            for col_idx in range(1, len(first_row) + 1):
                value = _cell_value(ws, header_row_idx, col_idx)
                headers.append(str(value or "").strip())
            if not any(headers):
                headers = [f"col_{i + 1}" for i in range(len(first_row))]
            blocks: list[ContentBlock] = []
            row_idx = 1  # header was row 1; data starts at row 2
            for row in rows_iter:
                row_idx += 1
                # Use `(row, col)` instead of `cell.row`/`cell.column` because
                # `EmptyCell` placeholders raise AttributeError on those attrs.
                values = [
                    _cell_value(ws, row_idx, col_idx)
                    for col_idx in range(1, len(headers) + 1)
                ]
                if all(v is None or str(v).strip() == "" for v in values):
                    continue
                rendered = _format_row(headers, values, row_idx)
                blocks.append(
                    ContentBlock(
                        kind="table",
                        text=rendered,
                        page=sheet_idx,
                    )
                )
            sections.append(
                ParsedSection(
                    heading=sheet_name or f"Sheet {sheet_idx + 1}",
                    level=1,
                    blocks=blocks,
                    page_start=sheet_idx,
                    page_end=sheet_idx,
                )
            )
        return ParsedDoc(
            document_name=path.name,
            format="xlsx",
            sections=sections,
            page_count=len(workbook.sheetnames),
        )
    finally:
        workbook.close()
