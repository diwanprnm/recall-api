"""
Structured logging — JSON to stdout, parsed by the OS/logger aggregator.
Import `structlog` instead of stdlib `logging` in all modules.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import Processor

from app.core.config import get_settings


def add_timestamp(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    import datetime
    event_dict["timestamp"] = datetime.datetime.utcnow().isoformat() + "Z"
    return event_dict


def configure_logging() -> None:
    """Call once at application startup."""
    cfg = get_settings()

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        add_timestamp,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if cfg.is_production:
        processors.append(structlog.processors.TimeStamper(fmt="iso"))
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Silence noisy third-party libraries
    for noisy_lib in ["httpx", "httpcore", "urllib3", "openai"]:
        logging.getLogger(noisy_lib).setLevel(logging.WARNING)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.DEBUG if cfg.debug else logging.INFO,
    )