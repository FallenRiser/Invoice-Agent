"""The human review queue -- anything not auto-posted lands here."""
from __future__ import annotations

from app.repositories.db import get_conn


def add_flag(source_file: str, decision: str, reason: str, extracted_json: str | None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO review_queue (source_file, decision, reason, extracted_json)
               VALUES (?, ?, ?, ?)""",
            (source_file, decision, reason, extracted_json),
        )
        return cur.lastrowid


def list_pending() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM review_queue WHERE status = 'PENDING' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def resolve(flag_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE review_queue SET status = 'RESOLVED', resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (flag_id,),
        )
