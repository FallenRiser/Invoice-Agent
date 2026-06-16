"""Duplicate detection + the record of every invoice successfully posted to Odoo.

Duplicate key = (normalized_vendor, invoice_number) -- the same vendor's
same invoice number arriving twice (e.g. a resend) must not be posted twice.
"""
from __future__ import annotations

from app.repositories.db import get_conn


def is_duplicate(normalized_vendor: str, invoice_number: str) -> bool:
    if not invoice_number:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_invoices WHERE normalized_vendor = ? AND invoice_number = ?",
            (normalized_vendor, invoice_number),
        ).fetchone()
        return row is not None


def record_processed(
    normalized_vendor: str, invoice_number: str, source_file: str, odoo_move_id: int | None
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO processed_invoices
               (normalized_vendor, invoice_number, source_file, odoo_move_id)
               VALUES (?, ?, ?, ?)""",
            (normalized_vendor, invoice_number, source_file, odoo_move_id),
        )
