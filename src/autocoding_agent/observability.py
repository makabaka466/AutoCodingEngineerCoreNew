"""Small local logging setup shared by every application interface."""

from __future__ import annotations

import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "autocoding_agent"
LOG_FILENAME = "autocoding-agent.log"
_handler_lock = threading.Lock()


def configure_file_logging(
    data_dir: str | Path,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 5,
) -> Path:
    """Write bounded UTF-8 logs below the application data directory."""

    log_dir = Path(data_dir).expanduser().resolve() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILENAME
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    with _handler_lock:
        for handler in list(logger.handlers):
            if not getattr(handler, "_autocoding_file_handler", False):
                continue
            if Path(handler.baseFilename) == log_path:
                return log_path
            logger.removeHandler(handler)
            handler.close()

        handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler._autocoding_file_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        logger.addHandler(handler)
        logger.info("logging_ready path=%s", log_path)
    return log_path
