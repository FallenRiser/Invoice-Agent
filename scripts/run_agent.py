"""
Entry point — starts the inbox watcher.

Usage:
    python scripts/run_agent.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make sure the project root is on the path so `app.*` imports resolve
# regardless of where the script is run from.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.core.logging_config import setup_logging
from app.watchers.inbox_watcher import InboxWatcher


def main() -> None:
    cfg = get_settings()

    setup_logging(
        log_dir  = str(cfg.paths.logs),
        log_file = cfg.paths.log_file,
    )

    import logging
    logger = logging.getLogger(__name__)
    logger.info("Starting Huma Invoice Processing Agent")
    logger.info("Project root : %s", cfg.project_root)
    logger.info("Inbox folder : %s", cfg.paths.inbox)
    logger.info("Config file  : config/settings.yaml")
    logger.info(
        "LLM          : %s  num_ctx=%d  temperature=%.1f",
        cfg.llm.model, cfg.llm.num_ctx, cfg.llm.temperature,
    )

    watcher = InboxWatcher(inbox_dir=cfg.paths.inbox)
    watcher.run_forever()


if __name__ == "__main__":
    main()
