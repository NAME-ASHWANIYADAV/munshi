"""Logging setup — one configuration point, used everywhere instead of ``print``."""

from __future__ import annotations

import logging
import sys
from typing import Any

__all__ = ["configure_logging", "get_logger"]

_CONFIGURED = False
_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_DATEFMT = "%H:%M:%S"


def configure_logging(level: str | int | None = None) -> None:
    """Install a console handler once. Safe to call repeatedly."""
    global _CONFIGURED
    if _CONFIGURED:
        if level is not None:
            logging.getLogger("munshiji").setLevel(level)
        return

    if level is None:
        from munshiji.config import get_settings

        level = get_settings().log_level

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(fmt=_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger("munshiji")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False

    # Vendor clients are chatty at DEBUG; keep them at WARNING unless we are debugging.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, configuring logging on first use."""
    configure_logging()
    if not name.startswith("munshiji"):
        name = f"munshiji.{name}"
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Log a structured one-line event: ``event key=value key=value``."""
    if not fields:
        logger.info(event)
        return
    rendered = " ".join(f"{key}={value!r}" for key, value in sorted(fields.items()))
    logger.info("%s %s", event, rendered)
