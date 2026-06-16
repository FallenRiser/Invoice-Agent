"""
PDF text + table extraction using pdfplumber.

Produces a structured, human-readable text dump:
  - Page headers
  - Tables rendered as aligned pipe-delimited grids
  - Plain paragraph text between/around tables

If the total extracted text is very short (< MIN_TEXT_CHARS) the PDF is
likely image-based (scanned). In that case `extract_from_pdf()` returns
an `ExtractionResult` with `is_image_based=True` so the caller can route
it to an OCR/LLM vision path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

logger = logging.getLogger(__name__)

# If total extracted text across all pages is below this, treat the PDF
# as image-based and flag it for LLM/OCR handling.
MIN_TEXT_CHARS = 10

# Stamp/watermark/document-type words that PDF letterheads often place
# visually near (overlapping or adjacent to) the vendor's company name --
# e.g. a "PAID" stamp graphic, or an "INVOICE" title positioned at the same
# vertical band as the letterhead. pdfplumber extracts text in reading
# order, not by visual grouping, so these can land as their own line right
# in the middle of the vendor's name (observed in practice: a real AWS
# invoice extracted as "Amazon Web Services EMEA INVOICE" / "SARL" / "PAID"
# -- a small LLM then misread "SARL" + "PAID" as the vendor name).
# Only whole lines that are EXACTLY one of these words (nothing else on the
# line) are dropped -- this can't accidentally eat real company/address
# text, since those never appear as a line containing only "PAID" etc.
_STANDALONE_STAMP_WORDS = {
    "invoice", "paid", "receipt", "draft", "copy", "original",
    "duplicate", "void", "confidential", "sample", "unpaid", "overdue",
}


def _strip_standalone_stamp_lines(text: str) -> str:
    """Drop lines whose entire (stripped, case-insensitive) content is a
    known stamp/label word -- see _STANDALONE_STAMP_WORDS."""
    kept = [
        line for line in text.split("\n")
        if line.strip().lower() not in _STANDALONE_STAMP_WORDS
    ]
    return "\n".join(kept)


@dataclass
class ExtractionResult:
    path: Path
    text: str = ""                      # full structured text output
    page_count: int = 0
    table_count: int = 0
    is_image_based: bool = False
    errors: list[str] = field(default_factory=list)


def _format_table(table: list[list[str | None]]) -> str:
    """Render a pdfplumber table as a pipe-delimited grid with aligned columns."""
    if not table:
        return ""

    # Normalise all cells to strings
    rows: list[list[str]] = [
        [str(cell).strip() if cell is not None else "" for cell in row]
        for row in table
    ]

    # Column widths
    col_count = max(len(r) for r in rows)
    widths = [0] * col_count
    for row in rows:
        for i, cell in enumerate(row):
            if i < col_count:
                widths[i] = max(widths[i], len(cell))

    def _render_row(row: list[str]) -> str:
        padded = [row[i].ljust(widths[i]) if i < len(row) else " " * widths[i]
                  for i in range(col_count)]
        return "| " + " | ".join(padded) + " |"

    separator = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    lines = [separator, _render_row(rows[0]), separator]
    for row in rows[1:]:
        lines.append(_render_row(row))
    lines.append(separator)
    return "\n".join(lines)


def extract_from_pdf(path: Path) -> ExtractionResult:
    """
    Extract all text and tables from *path*, returning a structured
    `ExtractionResult`.  Every step is logged so the terminal shows
    exactly what is happening page-by-page.
    """
    result = ExtractionResult(path=path)
    output_sections: list[str] = []

    logger.info("Opening PDF: %s", path.name)

    try:
        with pdfplumber.open(path) as pdf:
            result.page_count = len(pdf.pages)
            logger.info("PDF has %d page(s)", result.page_count)

            for page_num, page in enumerate(pdf.pages, start=1):
                logger.debug("Processing page %d / %d", page_num, result.page_count)
                page_sections: list[str] = [f"\n{'='*60}", f"  PAGE {page_num}", f"{'='*60}"]

                # ── Tables ─────────────────────────────────────────────
                tables = page.extract_tables()
                if tables:
                    logger.debug("  Found %d table(s) on page %d", len(tables), page_num)
                    for t_idx, table in enumerate(tables, start=1):
                        result.table_count += 1
                        page_sections.append(f"\n[Table {t_idx}]")
                        page_sections.append(_format_table(table))
                else:
                    logger.debug("  No tables on page %d", page_num)

                # ── Plain text ─────────────────────────────────────────
                # Use extract_text with layout=True to preserve spacing/columns
                plain = page.extract_text(x_tolerance=3, y_tolerance=3)
                if plain:
                    plain = _strip_standalone_stamp_lines(plain).strip()
                    if plain:
                        page_sections.append("\n[Text]")
                        page_sections.append(plain)

                output_sections.extend(page_sections)

    except Exception as exc:
        logger.error("Failed to open/parse PDF '%s': %s", path.name, exc)
        result.errors.append(str(exc))
        return result

    result.text = "\n".join(output_sections)

    # ── Image-based check ─────────────────────────────────────────────
    raw_len = len(result.text.replace("\n", "").replace(" ", "").replace("=", "").replace("-", "").replace("|", ""))
    if raw_len < MIN_TEXT_CHARS:
        result.is_image_based = True
        logger.warning(
            "Very little text extracted from '%s' (%d meaningful chars). "
            "PDF is likely image-based — will need LLM/OCR (not implemented yet).",
            path.name, raw_len,
        )
    else:
        logger.info(
            "Extraction complete: %d page(s), %d table(s), %d chars extracted",
            result.page_count, result.table_count, len(result.text),
        )

    return result
