"""
Plain sqlite3 -- no ORM. Trade-off, not an oversight: at this scale (a
handful of invoices arriving via a folder watcher) raw SQL is faster to get
right than wiring an ORM under a deadline.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.core.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_vendor TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    source_file TEXT NOT NULL,
    odoo_move_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(normalized_vendor, invoice_number)
);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    extracted_json TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def get_db_path() -> Path:
    path = get_settings().paths.db
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def get_conn():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    # SQLite's default rollback-journal mode needs proper file locking on the
    # *.db-journal sibling file. Some mounted/synced folders (network drives,
    # FUSE mounts, certain cloud-sync clients) don't support that and raise
    # "disk I/O error" on the very first write. MEMORY mode keeps the journal
    # in RAM instead, which sidesteps the issue -- an acceptable trade-off at
    # this app's write volume (a handful of invoices at a time), in exchange
    # for working reliably regardless of where data/agent.db happens to live.
    conn.execute("PRAGMA journal_mode=MEMORY")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
