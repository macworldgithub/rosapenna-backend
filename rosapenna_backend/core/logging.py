"""Configure the backend's console and rotating-file logger."""

import logging
from logging.handlers import RotatingFileHandler

from rosapenna_backend.config import settings


def configure_logging():
    logger = logging.getLogger("booking_api")
    logger.setLevel(settings.log_level.upper())
    logger.propagate = False
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    try:
        file_handler = RotatingFileHandler(settings.log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as exc:
        logger.warning("Could not attach file log handler (%s): %s", settings.log_file, exc)
    return logger


logger = configure_logging()
