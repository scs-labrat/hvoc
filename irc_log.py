"""Centralized logging configuration for veilid-irc.

Logs are written to ./logs/veilid-irc.log with daily rotation.
Each module gets its own named logger for easy filtering.

Usage in any module:
    from irc_log import get_logger
    log = get_logger(__name__)
    log.info("Something happened")
    log.debug("Detail: %s", value)
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "veilid-irc.log")
_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per file
_BACKUP_COUNT = 3               # keep 3 rotated files
_INITIALIZED = False


def _ensure_log_dir():
    """Create the logs/ directory if it doesn't exist."""
    os.makedirs(_LOG_DIR, exist_ok=True)


def _init_logging():
    """Set up the root logger with file and optional stderr handlers."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    _ensure_log_dir()

    # Root logger
    root = logging.getLogger("virc")
    root.setLevel(logging.DEBUG)

    # File handler — all levels, detailed format
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Also log WARNING+ to stderr so critical errors are visible
    # even if the TUI is running (Textual captures stdout)
    if os.environ.get("VIRC_STDERR_LOG"):
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.WARNING)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under the 'virc' namespace.

    Args:
        name: Module name (usually __name__). Will be prefixed with 'virc.'.

    Returns:
        A configured Logger instance.
    """
    _init_logging()
    # Strip common prefix for cleaner names
    short = name.replace("irc_", "").replace("__main__", "main")
    return logging.getLogger(f"virc.{short}")
