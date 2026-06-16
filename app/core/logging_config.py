"""
Terminal logging configuration.

Uses standard Python logging with a custom color formatter so every
log line is easy to scan at a glance:
  - timestamp in dim grey
  - level badge in color (green INFO, yellow WARNING, red ERROR)
  - logger name in cyan
  - message in default color

Call `setup_logging()` once at startup (from scripts/run_agent.py).
"""
from __future__ import annotations

import logging
import sys

# ANSI color codes
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_CYAN   = "\033[36m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_MAGENTA = "\033[35m"
_WHITE  = "\033[37m"

_LEVEL_COLORS = {
    "DEBUG":    _DIM + _WHITE,
    "INFO":     _GREEN,
    "WARNING":  _YELLOW,
    "ERROR":    _RED,
    "CRITICAL": _BOLD + _RED,
}


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level_color = _LEVEL_COLORS.get(record.levelname, _WHITE)
        ts          = self.formatTime(record, "%H:%M:%S")
        level_badge = f"{level_color}{record.levelname:<8}{_RESET}"
        name_badge  = f"{_CYAN}{record.name}{_RESET}"
        msg         = record.getMessage()

        # Include exception info if present
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)

        return f"{_DIM}{ts}{_RESET}  {level_badge}  {name_badge}  {msg}"


class _PlainFormatter(logging.Formatter):
    """No ANSI codes — used for the log file so it stays clean when opened in an editor."""
    def format(self, record: logging.LogRecord) -> str:
        ts  = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"{ts}  {record.levelname:<8}  {record.name}  {msg}"


def setup_logging(
    level: int = logging.DEBUG,
    log_dir: str = "logs",
    log_file: str = "agent.log",
    max_bytes: int = 5 * 1024 * 1024,   # 5 MB per file
    backup_count: int = 5,               # keep agent.log.1 … agent.log.5
) -> None:
    """
    Configure root logger with:
      - Colored output to stdout (terminal)
      - Plain rotating file output to logs/agent.log
    """
    import io
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    # ── Terminal handler (color, UTF-8) ───────────────────────────────
    stdout_utf8 = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    stream_handler = logging.StreamHandler(stdout_utf8)
    stream_handler.setFormatter(_ColorFormatter())

    # ── File handler (plain text, rotating) ───────────────────────────
    log_path = Path(log_dir) / log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(_PlainFormatter())

    # ── Root logger ───────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(stream_handler)
    root.addHandler(file_handler)

    # Suppress noisy third-party loggers
    logging.getLogger("watchdog").setLevel(logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Log where the file is going so it's obvious on startup
    logging.getLogger(__name__).info("Log file: %s", log_path.resolve())
