"""Application logging with a bounded on-disk history for Edge devices."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "data" / "logs"


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure logging once and return the Vision Office root logger."""
    logger = logging.getLogger("vision_office")
    if logger.handlers:
        return logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(processName)s %(name)s: %(message)s"
    )

    file_handler = RotatingFileHandler(
        LOG_DIR / "vision-office.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger
