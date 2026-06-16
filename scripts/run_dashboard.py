#!/usr/bin/env python3
"""CLI entry point: `python scripts/run_dashboard.py`.

Starts the review dashboard (app/web/dashboard.py) at http://127.0.0.1:8001. Reads/writes
the exact same data/agent.db and config/settings.yaml as
scripts/run_agent.py -- run them side by side, nothing to keep in sync. Port 8001 since
Odoo already owns 8069.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make sure the project root is on the path so `app.*` imports resolve
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn

from app.core.config import get_settings
from app.core.logging_config import setup_logging

if __name__ == "__main__":
    cfg = get_settings()
    setup_logging(
        log_dir  = str(cfg.paths.logs),
        log_file = "dashboard.log",
    )
    print("Review dashboard: http://127.0.0.1:8001")
    uvicorn.run("app.web.dashboard:app", host="127.0.0.1", port=8001, reload=False)
