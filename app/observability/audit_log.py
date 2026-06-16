"""
Every decision the agent makes -- POSTED, INCOMPLETE, NEW_VENDOR_SE,
DUPLICATE, MATH_MISMATCH, EXTRACTION_FAILED -- gets one row here, regardless
of outcome. This is the "why did the agent do X" answer for a human reviewer.
"""
from __future__ import annotations

import logging

from app.repositories.db import get_conn

logger = logging.getLogger(__name__)


def write(source_file: str, decision: str, reason: str, detail: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (source_file, decision, reason, detail) VALUES (?, ?, ?, ?)",
            (source_file, decision, reason, detail),
        )
    logger.info("[AUDIT] %-14s %s -- %s", decision, source_file, reason)


def list_all() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]
