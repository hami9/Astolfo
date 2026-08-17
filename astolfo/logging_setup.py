"""Logging configuration."""

from __future__ import annotations

import logging
import os

NOISY = ("httpx", "httpcore", "telegram.ext", "apscheduler", "PIL")


def configure(level: str | None = None) -> None:
    resolved = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        format="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
        datefmt="%H:%M:%S",
        level=getattr(logging, resolved, logging.INFO),
    )
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
