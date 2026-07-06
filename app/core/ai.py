"""
Instructor client for AI pipeline — powered by 9router (OpenAI-compatible).

Instructor provides structured LLM output (Pydantic models) with automatic
validation and retries. This saves 50-70% boilerplate vs raw chat completions.

Key principle from IDEATION-CANVAS: ONE LLM call does everything:
  classification + summarization + entity extraction + sentiment + tags.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import instructor
import structlog
from openai import AsyncOpenAI, OpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import get_settings

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

# ── Module-level singletons ────────────────────────────────────────────────────

_async_client: AsyncOpenAI | None = None
_sync_client: OpenAI | None = None


def _init_clients() -> tuple[AsyncOpenAI, OpenAI]:
    """Initialise both async and sync clients with instructor patch."""
    cfg = get_settings()

    base = OpenAI(
        api_key=cfg.openai_api_key,
        base_url=cfg.openai_base_url,
        timeout=60.0,
        max_retries=0,  # we handle retries via tenacity
    )

    # instructor.patch() adds .messages.create() that returns a Pydantic model
    async_client = instructor.from_openai(
        base,
        mode=instructor.Mode.JSON,  # enforce JSON mode for reliability
    )
    sync_client = instructor.from_openai(
        base,
        mode=instructor.Mode.JSON,
    )

    return async_client, sync_client


def get_async_instructor() -> instructor.AsyncInstructor:
    global _async_client
    if _async_client is None:
        _async_client, _sync_client = _init_clients()
        logger.info(
            "Instructor/9router client initialised",
            extra={
                "base_url": get_settings().openai_base_url,
                "model": get_settings().ai_model,
            },
        )
    return _async_client  # type: ignore[return-value]


def get_sync_instructor() -> instructor.Instructor:
    global _sync_client
    if _sync_client is None:
        _async_client, _sync_client = _init_clients()
    return _sync_client  # type: ignore[return-value]


# ── Reusable retry decorator for LLM calls ────────────────────────────────────
# Transient errors (network blips, 429 rate limits) should be retried.
# We DON'T retry on validation errors (malformed output) — those need a code fix.

def _is_transient_error(exc: Exception) -> bool:
    """Returns True if the exception is a transient error worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    if isinstance(exc, httpx.TimeoutException):
        return True
    return False


retry_on_transient = retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
    before_sleep=lambda retry_state: logger.warning(
        "Retrying AI call after error",
        extra={"attempt": retry_state.attempt_number, "exc": retry_state.outcome.exception()},
    ),
)
