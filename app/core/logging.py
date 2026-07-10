"""
Structured logging — JSON to stdout, parsed by the OS/logger aggregator.
Import `structlog` instead of stdlib `logging` in all modules.
"""
from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import structlog

from app.core.config import get_settings

if TYPE_CHECKING:
    from structlog.types import Processor

# Silence noisy third-party libraries
for _noisy_lib in ["httpx", "httpcore", "urllib3", "openai"]:
    logging.getLogger(_noisy_lib).setLevel(logging.WARNING)


def configure_logging() -> None:
    """Call once at application startup."""
    cfg = get_settings()

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if cfg.is_production:
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

    # Production: use WARNING as root level so DEBUG/INFO from structlog
    # (which uses stdlib LoggerFactory) still gets through via structlog processors.
    # For third-party libs, WARNING is appropriate.
    # For our app code: structlog handles level filtering via its own processors.
    root_level = logging.DEBUG if cfg.debug else logging.WARNING

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=root_level,
    )

    # Ensure our app modules log at INFO level even in production
    logging.getLogger("app").setLevel(logging.INFO)
