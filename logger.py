"""
Unified logging configuration for Binance Spot Trading Bot.
Provides timestamped logging to console and a rotating file at logs/spot_bot.log.
"""

import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Directory setup
BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOG_FILE_PATH = LOGS_DIR / "spot_bot.log"

LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s]: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_root_logging(level: int = logging.INFO) -> None:
    """Configures console and rotating file handlers."""
    global _configured
    if _configured:
        return

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        filename=LOG_FILE_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger("spot_bot")
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger.propagate = False

    _configured = True


def get_logger(name: str = "spot_bot") -> logging.Logger:
    """Returns a configured logger instance within the spot_bot namespace."""
    if not _configured:
        setup_root_logging()

    if not name.startswith("spot_bot"):
        logger_name = f"spot_bot.{name}"
    else:
        logger_name = name

    return logging.getLogger(logger_name)
